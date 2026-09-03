#!/usr/bin/env python3
"""Evaluate a frozen cross-format semantic graph without exposing gold to runtime.

The ordering in ``run_evaluation`` is a security boundary:

1. build a question-independent graph from already-safe Phase 1 artifacts;
2. freeze and independently validate that SQLite snapshot;
3. only then read evaluator-only graph and QA gold;
4. give the answerer an input file containing exactly ``{"question": ...}``;
5. match runtime Edge tuples to gold and audit every answer trace afterwards.

Builder and answerer subprocesses run under a small ``sitecustomize`` socket
guard.  Any outbound socket or DNS attempt is recorded, blocked, and fails the
evaluation.  This complements, rather than trusts, runtime self-reporting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote


SCHEMA_VERSION = "0.1"
GRAPH_SNAPSHOT_PREFIX = "xkgs_"
RUN_PREFIX = "xkgr_"
EXPECTED_BUILDER = "cross-document-semantic-graph-builder"
REQUIRED_GRAPH_TABLES = {
    "metadata",
    "source_evidence",
    "nodes",
    "edges",
    "edge_evidence",
}
ANSWER_KEYS = {
    "schema_version",
    "record_type",
    "answerer",
    "answerer_version",
    "question_hash",
    "operation",
    "decision",
    "reason_code",
    "must_request_concept",
    "answer_text",
    "asserted_facts",
    "asserted_relations",
    "trace",
}
TRACE_KEYS = {
    "run_id",
    "graph_snapshot_id",
    "question_hash",
    "visited_node_ids",
    "visited_node_hashes",
    "visited_edge_ids",
    "visited_edge_hashes",
    "used_semantic_edge_ids",
    "used_semantic_edge_count",
    "used_edge_statuses",
    "visited_document_paths",
    "resolved_source_references",
    "disabled_edge_ids",
    "decision",
    "elapsed_ms",
    "peak_rss_bytes",
    "outbound_network_attempt_count",
}
EVALUATOR_ONLY_KEYS = {
    "qa_case_id",
    "gold_edge_key",
    "required_gold_edge_keys",
    "expected",
    "graph_requirements",
    "provenance",
}


class EvaluationError(ValueError):
    """Raised when an evaluation contract or result is invalid."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def normalize_text(value: str) -> str:
    if not isinstance(value, str):
        raise EvaluationError("text value must be a string")
    return unicodedata.normalize("NFC", value).strip()


def question_hash(question: str) -> str:
    return sha256_text(normalize_text(question))


def _json_object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"{label}: invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise EvaluationError(f"{label}: JSON object required")
    return parsed


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"JSON object required: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise EvaluationError(
                        f"JSON object required at {path}:{line_number}"
                    )
                records.append(value)
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"invalid JSONL: {path}:{exc.lineno}") from exc
    except OSError as exc:
        raise EvaluationError(f"cannot read JSONL: {path}") from exc
    if not records:
        raise EvaluationError(f"JSONL is empty: {path}")
    return records


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(canonical_json(dict(value)) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(canonical_json(dict(record)) + "\n")


@dataclass(frozen=True)
class Node:
    node_id: str
    node_type: str
    canonical_key: str
    status: str
    properties: dict[str, Any]
    record_sha256: str


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    document_id: str
    relative_path: str
    source_sha256: str
    locator: dict[str, Any]
    observed_text: str
    observed_sha256: str
    record_sha256: str


@dataclass(frozen=True)
class Edge:
    edge_id: str
    from_node_id: str
    relation_type: str
    to_node_id: str
    relation_class: str
    status: str
    basis_kind: str
    basis_rule: str
    properties: dict[str, Any]
    supporting_evidence_ids: tuple[str, ...]
    record_sha256: str


@dataclass(frozen=True)
class GraphSnapshot:
    graph_snapshot_id: str
    logical_snapshot_sha256: str
    builder: str
    builder_version: str
    documents_input_sha256: str
    evidence_input_sha256: str
    nodes: dict[str, Node]
    edges: dict[str, Edge]
    evidence: dict[str, Evidence]

    @classmethod
    def load(cls, path: Path) -> "GraphSnapshot":
        if not path.is_file():
            raise EvaluationError(f"graph SQLite is missing: {path}")
        uri = f"file:{quote(str(path.resolve()))}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            actual_tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing = sorted(REQUIRED_GRAPH_TABLES - actual_tables)
            if missing:
                raise EvaluationError(f"graph SQLite missing tables: {missing}")

            metadata: dict[str, Any] = {}
            for row in connection.execute("SELECT key, value FROM metadata"):
                try:
                    metadata[row["key"]] = json.loads(row["value"])
                except json.JSONDecodeError as exc:
                    raise EvaluationError(
                        f"metadata value is not canonical JSON: {row['key']}"
                    ) from exc
            required_metadata = {
                "schema_version": SCHEMA_VERSION,
                "builder": EXPECTED_BUILDER,
                "status": "complete",
                "question_independent": True,
                "external_network_used": False,
            }
            for key, expected in required_metadata.items():
                if metadata.get(key) != expected:
                    raise EvaluationError(
                        f"metadata {key} must equal {expected!r}"
                    )
            if not isinstance(metadata.get("builder_version"), str):
                raise EvaluationError("metadata builder_version is missing")
            for key in ("documents_input_sha256", "evidence_input_sha256"):
                if not _is_sha256(metadata.get(key)):
                    raise EvaluationError(f"metadata {key} is invalid")
            stored_snapshot_id = metadata.get("graph_snapshot_id")
            if not isinstance(stored_snapshot_id, str) or not stored_snapshot_id.startswith(
                GRAPH_SNAPSHOT_PREFIX
            ):
                raise EvaluationError("graph_snapshot_id is missing or invalid")

            evidence: dict[str, Evidence] = {}
            for row in connection.execute(
                "SELECT evidence_id, document_id, relative_path, source_sha256, "
                "locator_json, observed_text, observed_sha256, record_sha256 "
                "FROM source_evidence "
                "ORDER BY evidence_id"
            ):
                evidence_id = row["evidence_id"]
                if evidence_id in evidence:
                    raise EvaluationError(f"duplicate source Evidence: {evidence_id}")
                observed_text = row["observed_text"]
                if sha256_text(observed_text) != row["observed_sha256"]:
                    raise EvaluationError(
                        f"source Evidence text hash mismatch: {evidence_id}"
                    )
                locator = _json_object(row["locator_json"], f"{evidence_id}.locator")
                evidence_payload = {
                    "evidence_id": evidence_id,
                    "document_id": row["document_id"],
                    "relative_path": row["relative_path"],
                    "source_sha256": row["source_sha256"],
                    "locator": locator,
                    "observed_text": observed_text,
                    "observed_sha256": row["observed_sha256"],
                }
                if sha256_value(evidence_payload) != row["record_sha256"]:
                    raise EvaluationError(
                        f"source Evidence record hash mismatch: {evidence_id}"
                    )
                evidence[evidence_id] = Evidence(
                    evidence_id=evidence_id,
                    document_id=row["document_id"],
                    relative_path=row["relative_path"],
                    source_sha256=row["source_sha256"],
                    locator=locator,
                    observed_text=observed_text,
                    observed_sha256=row["observed_sha256"],
                    record_sha256=row["record_sha256"],
                )

            nodes: dict[str, Node] = {}
            for row in connection.execute(
                "SELECT node_id, node_type, canonical_key, status, properties_json, "
                "record_sha256 FROM nodes ORDER BY node_id"
            ):
                node_id = row["node_id"]
                properties = _json_object(row["properties_json"], f"{node_id}.properties")
                payload = {
                    "node_id": node_id,
                    "node_type": row["node_type"],
                    "canonical_key": row["canonical_key"],
                    "status": row["status"],
                    "properties": properties,
                }
                if sha256_value(payload) != row["record_sha256"]:
                    raise EvaluationError(f"Node hash mismatch: {node_id}")
                if node_id in nodes:
                    raise EvaluationError(f"duplicate Node: {node_id}")
                if row["status"] != "verified":
                    raise EvaluationError(f"graph contains non-verified Node: {node_id}")
                nodes[node_id] = Node(
                    node_id=node_id,
                    node_type=row["node_type"],
                    canonical_key=row["canonical_key"],
                    status=row["status"],
                    properties=properties,
                    record_sha256=row["record_sha256"],
                )

            edge_support: dict[str, list[str]] = {}
            for row in connection.execute(
                "SELECT edge_id, evidence_id FROM edge_evidence "
                "ORDER BY edge_id, evidence_id"
            ):
                evidence_id = row["evidence_id"]
                if evidence_id not in evidence:
                    raise EvaluationError(
                        f"Edge references unknown Evidence: {row['edge_id']}"
                    )
                edge_support.setdefault(row["edge_id"], []).append(evidence_id)

            edges: dict[str, Edge] = {}
            for row in connection.execute(
                "SELECT edge_id, from_node_id, relation_type, to_node_id, "
                "relation_class, status, basis_kind, basis_rule, properties_json, "
                "record_sha256 FROM edges ORDER BY edge_id"
            ):
                edge_id = row["edge_id"]
                support_ids = tuple(sorted(edge_support.get(edge_id, [])))
                properties = _json_object(row["properties_json"], f"{edge_id}.properties")
                payload = {
                    "edge_id": edge_id,
                    "from_node_id": row["from_node_id"],
                    "relation_type": row["relation_type"],
                    "to_node_id": row["to_node_id"],
                    "relation_class": row["relation_class"],
                    "status": row["status"],
                    "basis_kind": row["basis_kind"],
                    "basis_rule": row["basis_rule"],
                    "properties": properties,
                    "supporting_evidence_ids": list(support_ids),
                }
                if sha256_value(payload) != row["record_sha256"]:
                    raise EvaluationError(f"Edge hash mismatch: {edge_id}")
                if edge_id in edges:
                    raise EvaluationError(f"duplicate Edge: {edge_id}")
                if row["from_node_id"] not in nodes or row["to_node_id"] not in nodes:
                    raise EvaluationError(f"Edge endpoint is unknown: {edge_id}")
                if not support_ids:
                    raise EvaluationError(f"Edge has no supporting Evidence: {edge_id}")
                edges[edge_id] = Edge(
                    edge_id=edge_id,
                    from_node_id=row["from_node_id"],
                    relation_type=row["relation_type"],
                    to_node_id=row["to_node_id"],
                    relation_class=row["relation_class"],
                    status=row["status"],
                    basis_kind=row["basis_kind"],
                    basis_rule=row["basis_rule"],
                    properties=properties,
                    supporting_evidence_ids=support_ids,
                    record_sha256=row["record_sha256"],
                )

            dangling_support = sorted(set(edge_support) - set(edges))
            if dangling_support:
                raise EvaluationError(
                    f"edge_evidence references unknown Edges: {dangling_support}"
                )
            snapshot_payload = {
                "evidence_record_sha256": sorted(
                    item.record_sha256 for item in evidence.values()
                ),
                "node_record_sha256": sorted(
                    node.record_sha256 for node in nodes.values()
                ),
                "edge_record_sha256": sorted(
                    edge.record_sha256 for edge in edges.values()
                ),
            }
            logical_sha = sha256_value(snapshot_payload)
            computed_snapshot_id = GRAPH_SNAPSHOT_PREFIX + logical_sha[:32]
            if computed_snapshot_id != stored_snapshot_id:
                raise EvaluationError("logical graph snapshot hash does not match metadata")
            metadata_logical = metadata.get("logical_snapshot_sha256")
            if metadata_logical != logical_sha:
                raise EvaluationError("logical_snapshot_sha256 does not match graph records")
            expected_counts = {
                "document_count": len({item.document_id for item in evidence.values()}),
                "source_evidence_count": len(evidence),
                "node_count": len(nodes),
                "edge_count": len(edges),
            }
            for key, expected in expected_counts.items():
                if type(metadata.get(key)) is not int or metadata[key] != expected:
                    raise EvaluationError(f"metadata {key} does not match graph")
            return cls(
                graph_snapshot_id=stored_snapshot_id,
                logical_snapshot_sha256=metadata_logical,
                builder=metadata["builder"],
                builder_version=metadata["builder_version"],
                documents_input_sha256=metadata["documents_input_sha256"],
                evidence_input_sha256=metadata["evidence_input_sha256"],
                nodes=nodes,
                edges=edges,
                evidence=evidence,
            )
        except sqlite3.Error as exc:
            raise EvaluationError(f"cannot validate graph SQLite: {exc}") from exc
        finally:
            connection.close()


@dataclass(frozen=True)
class GuardedRun:
    argv: tuple[str, ...]
    stdout: str
    stderr: str
    network_attempt_count: int


class NetworkGuard:
    """Prepare and verify a subprocess-local Python socket guard."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.module_dir = root / "network-guard"
        self.audit_dir = root / "network-audit"
        self.module_dir.mkdir(parents=True, exist_ok=False)
        self.audit_dir.mkdir(parents=True, exist_ok=False)
        self._sequence = 0
        source = '''import os
import socket

_attempt_path = os.environ.get("CROSS_FORMAT_NETWORK_ATTEMPT_LOG")
_marker_path = os.environ.get("CROSS_FORMAT_NETWORK_GUARD_MARKER")

def _append(path, value):
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(value + "\\n")

_append(_marker_path, "loaded")

def _blocked(operation, value):
    _append(_attempt_path, operation + ":" + repr(value))
    raise RuntimeError("outbound network is disabled by Phase 2 evaluator")

class _GuardedSocket(socket.socket):
    def connect(self, address):
        return _blocked("socket.connect", address)
    def connect_ex(self, address):
        return _blocked("socket.connect_ex", address)
    def sendto(self, *args, **kwargs):
        return _blocked("socket.sendto", args)

def _guarded_create_connection(address, *args, **kwargs):
    return _blocked("socket.create_connection", address)

def _guarded_getaddrinfo(host, port, *args, **kwargs):
    return _blocked("socket.getaddrinfo", (host, port))

socket.socket = _GuardedSocket
socket.create_connection = _guarded_create_connection
socket.getaddrinfo = _guarded_getaddrinfo
'''
        (self.module_dir / "sitecustomize.py").write_text(source, encoding="utf-8")

    def run(self, argv: Sequence[str], *, cwd: Path) -> GuardedRun:
        self._sequence += 1
        label = f"process-{self._sequence:04d}"
        marker = self.audit_dir / f"{label}.loaded"
        attempts = self.audit_dir / f"{label}.attempts"
        environment = dict(os.environ)
        for key in list(environment):
            upper = key.upper()
            if any(token in upper for token in ("GOLD", "QA_CASE", "EXPECTED")):
                environment.pop(key, None)
        environment["PYTHONPATH"] = str(self.module_dir)
        environment["CROSS_FORMAT_NETWORK_ATTEMPT_LOG"] = str(attempts)
        environment["CROSS_FORMAT_NETWORK_GUARD_MARKER"] = str(marker)
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        ):
            environment.pop(key, None)
        environment["NO_PROXY"] = "*"
        environment["no_proxy"] = "*"
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        loaded = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
        attempt_lines = (
            attempts.read_text(encoding="utf-8").splitlines()
            if attempts.exists()
            else []
        )
        if loaded != ["loaded"]:
            raise EvaluationError(f"network guard did not load exactly once: {argv[0]}")
        if attempt_lines:
            raise EvaluationError(
                f"outbound network attempt blocked ({len(attempt_lines)}): "
                f"{attempt_lines[0]}"
            )
        if completed.returncode != 0:
            raise EvaluationError(
                f"subprocess failed ({completed.returncode}): {' '.join(argv)}\n"
                f"stdout: {completed.stdout.strip()}\n"
                f"stderr: {completed.stderr.strip()}"
            )
        return GuardedRun(
            argv=tuple(argv),
            stdout=completed.stdout,
            stderr=completed.stderr,
            network_attempt_count=0,
        )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _ensure_runtime_boundary(
    argv: Sequence[str], *, dataset: Path, allowed_question_file: Path | None = None
) -> None:
    """Reject explicit evaluator-only paths in child-process arguments."""

    evaluator_only = [
        dataset / "gold",
        dataset / "fixture-spec.json",
        dataset / "corpus-manifest.json",
        dataset / "cases.jsonl",
    ]
    if allowed_question_file is not None and _is_relative_to(
        allowed_question_file, dataset
    ):
        raise EvaluationError("answerer question envelope must remain outside dataset")
    for raw in argv:
        if not isinstance(raw, str) or not raw:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            continue
        for forbidden in evaluator_only:
            if candidate.resolve() == forbidden.resolve() or _is_relative_to(
                candidate, forbidden
            ):
                raise EvaluationError(
                    f"evaluator-only path exposed to runtime subprocess: {candidate}"
                )


@dataclass(frozen=True)
class FrozenBuild:
    graph_path: Path
    state_path: Path
    snapshot: GraphSnapshot
    graph_file_sha256: str
    builder_run: GuardedRun


def build_and_freeze(
    *,
    phase1_dir: Path,
    dataset: Path,
    builder: Path,
    python: Path,
    staging: Path,
    guard: NetworkGuard,
) -> FrozenBuild:
    """Build only from safe Phase 1 files, then freeze before any gold read."""

    documents = phase1_dir / "semantic-documents.jsonl"
    evidence = phase1_dir / "safe-answer-evidence.jsonl"
    for path in (documents, evidence, builder, python):
        if not path.is_file():
            raise EvaluationError(f"required build input is missing: {path}")
    if _is_relative_to(phase1_dir, dataset):
        raise EvaluationError("Phase 1 safe output must be outside evaluator dataset")

    graph_path = staging / "semantic-graph.sqlite3"
    state_path = staging / "semantic-graph-state.json"
    argv = [
        str(python.resolve()),
        str(builder.resolve()),
        "--documents",
        str(documents.resolve()),
        "--evidence",
        str(evidence.resolve()),
        "--output",
        str(graph_path.resolve()),
        "--state",
        str(state_path.resolve()),
    ]
    _ensure_runtime_boundary(argv, dataset=dataset)
    builder_run = guard.run(argv, cwd=staging)
    if not graph_path.is_file() or not state_path.is_file():
        raise EvaluationError("builder did not publish graph and state outputs")

    snapshot = GraphSnapshot.load(graph_path)
    state = _read_json(state_path)
    if state.get("graph_snapshot_id") != snapshot.graph_snapshot_id:
        raise EvaluationError("builder state graph_snapshot_id mismatch")
    if state.get("question_independent") is not True:
        raise EvaluationError("builder state does not prove question-independent build")
    if state.get("external_network_used") is not False:
        raise EvaluationError("builder state does not prove offline execution")
    if state.get("outbound_network_attempt_count", 0) != 0:
        raise EvaluationError("builder reported an outbound network attempt")

    graph_file_sha256 = sha256_file(graph_path)
    if snapshot.documents_input_sha256 != sha256_file(documents):
        raise EvaluationError("graph metadata documents input hash mismatch")
    if snapshot.evidence_input_sha256 != sha256_file(evidence):
        raise EvaluationError("graph metadata Evidence input hash mismatch")
    if state.get("logical_snapshot_sha256") != snapshot.logical_snapshot_sha256:
        raise EvaluationError("builder state logical snapshot hash mismatch")
    if state.get("sqlite_sha256") != graph_file_sha256:
        raise EvaluationError("builder state SQLite file hash mismatch")
    expected_counts = {
        "documents": len({item.document_id for item in snapshot.evidence.values()}),
        "source_evidence": len(snapshot.evidence),
        "nodes": len(snapshot.nodes),
        "edges": len(snapshot.edges),
    }
    state_counts = state.get("counts")
    if not isinstance(state_counts, dict):
        raise EvaluationError("builder state graph counts are missing")
    _json_subset(expected_counts, state_counts, "builder_state.counts")
    graph_path.chmod(0o444)
    freeze_record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "cross_document_semantic_graph_freeze",
        "graph_snapshot_id": snapshot.graph_snapshot_id,
        "logical_snapshot_sha256": snapshot.logical_snapshot_sha256,
        "graph_file_sha256": graph_file_sha256,
        "question_independent": True,
        "gold_loaded": False,
        "outbound_network_attempt_count": builder_run.network_attempt_count,
    }
    _write_json(staging / "graph-freeze.json", freeze_record)
    return FrozenBuild(
        graph_path=graph_path,
        state_path=state_path,
        snapshot=snapshot,
        graph_file_sha256=graph_file_sha256,
        builder_run=builder_run,
    )


def _edge_tuple(
    from_type: str,
    from_key: str,
    relation_type: str,
    to_type: str,
    to_key: str,
) -> tuple[str, str, str, str, str]:
    return (
        normalize_text(from_type),
        normalize_text(from_key),
        normalize_text(relation_type).upper(),
        normalize_text(to_type),
        normalize_text(to_key),
    )


def _runtime_edge_tuple(snapshot: GraphSnapshot, edge: Edge) -> tuple[str, ...]:
    source = snapshot.nodes[edge.from_node_id]
    target = snapshot.nodes[edge.to_node_id]
    return _edge_tuple(
        source.node_type,
        source.canonical_key,
        edge.relation_type,
        target.node_type,
        target.canonical_key,
    )


def _gold_edge_tuple(record: Mapping[str, Any]) -> tuple[str, ...]:
    source = record.get("from")
    target = record.get("to")
    if not isinstance(source, dict) or not isinstance(target, dict):
        raise EvaluationError("gold Edge from/to must be objects")
    return _edge_tuple(
        source.get("node_type"),
        source.get("canonical_key"),
        record.get("relation_type"),
        target.get("node_type"),
        target.get("canonical_key"),
    )


def _json_subset(expected: Any, actual: Any, label: str) -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise EvaluationError(f"{label}: object required")
        for key, value in expected.items():
            if key not in actual:
                raise EvaluationError(f"{label}.{key}: property is missing")
            _json_subset(value, actual[key], f"{label}.{key}")
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            raise EvaluationError(f"{label}: list mismatch")
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual)):
            _json_subset(expected_item, actual_item, f"{label}[{index}]")
        return
    if type(expected) is not type(actual) or expected != actual:
        raise EvaluationError(f"{label}: expected {expected!r}, got {actual!r}")


def match_gold_edges(
    snapshot: GraphSnapshot, gold_edges: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    """Map evaluator-only gold keys to runtime IDs after snapshot freeze."""

    runtime_index: dict[tuple[str, ...], list[Edge]] = {}
    for edge in snapshot.edges.values():
        runtime_index.setdefault(_runtime_edge_tuple(snapshot, edge), []).append(edge)

    mapping: dict[str, str] = {}
    seen_gold_tuples: set[tuple[str, ...]] = set()
    for record in gold_edges:
        gold_key = record.get("gold_edge_key")
        if not isinstance(gold_key, str) or not gold_key:
            raise EvaluationError("gold Edge key is missing")
        if gold_key in mapping:
            raise EvaluationError(f"duplicate gold Edge key: {gold_key}")
        edge_tuple = _gold_edge_tuple(record)
        if edge_tuple in seen_gold_tuples:
            raise EvaluationError(f"duplicate gold Edge tuple: {edge_tuple}")
        seen_gold_tuples.add(edge_tuple)
        candidates = runtime_index.get(edge_tuple, [])
        if len(candidates) != 1:
            raise EvaluationError(
                f"gold Edge tuple must match exactly once ({gold_key}); got {len(candidates)}"
            )
        edge = candidates[0]
        if edge.relation_class != "semantic":
            raise EvaluationError(f"gold Edge is not semantic at runtime: {gold_key}")
        if edge.status != record.get("expected_status"):
            raise EvaluationError(f"gold Edge status mismatch: {gold_key}")
        expected_properties = record.get("properties")
        if not isinstance(expected_properties, dict):
            raise EvaluationError(f"gold Edge properties must be an object: {gold_key}")
        _json_subset(expected_properties, edge.properties, f"{gold_key}.properties")

        references = record.get("source_references")
        if not isinstance(references, list) or not references:
            raise EvaluationError(f"gold Edge has no source references: {gold_key}")
        support = [snapshot.evidence[item] for item in edge.supporting_evidence_ids]
        for index, reference in enumerate(references):
            if not isinstance(reference, dict):
                raise EvaluationError(f"invalid source reference: {gold_key}[{index}]")
            selector = reference.get("selector")
            if not isinstance(selector, dict) or selector.get("kind") != "exact_phrase":
                raise EvaluationError(f"exact_phrase selector required: {gold_key}")
            path = reference.get("path")
            phrase = selector.get("value")
            if not isinstance(path, str) or not isinstance(phrase, str) or not phrase:
                raise EvaluationError(f"path and exact phrase required: {gold_key}")
            matches = [
                item
                for item in support
                if item.relative_path == path
                and normalize_text(phrase) in normalize_text(item.observed_text)
            ]
            if not matches:
                raise EvaluationError(
                    f"gold source reference is not resolved by supporting Evidence: "
                    f"{gold_key}[{index}]"
                )
        mapping[gold_key] = edge.edge_id
    return mapping


def _expected_source_references(
    snapshot: GraphSnapshot, used_edge_ids: Sequence[str]
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for edge_id in used_edge_ids:
        edge = snapshot.edges[edge_id]
        for evidence_id in edge.supporting_evidence_ids:
            evidence = snapshot.evidence[evidence_id]
            references.append({
                "edge_id": edge_id,
                "evidence_id": evidence_id,
                "document_id": evidence.document_id,
                "path": evidence.relative_path,
                "source_sha256": evidence.source_sha256,
                "locator": evidence.locator,
                "observed_text_sha256": evidence.observed_sha256,
                "quote": evidence.observed_text,
            })
    return sorted(references, key=lambda item: (item["edge_id"], item["evidence_id"]))


def _reject_evaluator_keys(value: Any, label: str = "answer") -> None:
    if isinstance(value, dict):
        leaked = sorted(EVALUATOR_ONLY_KEYS & set(value))
        if leaked:
            raise EvaluationError(f"evaluator-only keys leaked into {label}: {leaked}")
        for key, child in value.items():
            _reject_evaluator_keys(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_evaluator_keys(child, f"{label}[{index}]")


def _edge_proves_fact(
    snapshot: GraphSnapshot, edge: Edge, field: str, value: Any
) -> bool:
    target = snapshot.nodes[edge.to_node_id]
    if field == "reference_time" and isinstance(value, str):
        try:
            reference = date.fromisoformat(value)
            start_raw = edge.properties.get("valid_from")
            end_raw = edge.properties.get("valid_to")
            start = date.fromisoformat(start_raw) if isinstance(start_raw, str) else None
            end = date.fromisoformat(end_raw) if isinstance(end_raw, str) else None
        except ValueError:
            return False
        return start is not None and start <= reference and (end is None or reference <= end)
    if field == "role":
        return edge.properties.get("role") == value
    if field.endswith("assignee_id") or field == "assignee_id":
        return target.node_type == "Employee" and target.canonical_key == value
    if field.endswith("assignee_name") or field == "assignee_name":
        return target.node_type == "Person" and target.canonical_key == value
    if field == "change_reason":
        return target.node_type == "Reason" and target.canonical_key == value
    property_candidates = {
        "change_effective_date": ("valid_from", "effective_from"),
        "previous_valid_to": ("valid_to",),
        "effective_from": ("effective_from",),
        "old_plan_status": ("claim_status",),
        "current_plan_status": ("claim_status",),
    }.get(field, (field,))
    if any(edge.properties.get(key) == value for key in property_candidates):
        return True
    return value in {
        snapshot.nodes[edge.from_node_id].canonical_key,
        target.canonical_key,
    }


def _assert_used_graph_connected(snapshot: GraphSnapshot, edge_ids: Sequence[str]) -> None:
    if not edge_ids:
        return
    adjacency: dict[str, set[str]] = {}
    for edge_id in edge_ids:
        edge = snapshot.edges[edge_id]
        adjacency.setdefault(edge.from_node_id, set()).add(edge.to_node_id)
        adjacency.setdefault(edge.to_node_id, set()).add(edge.from_node_id)
    pending = [next(iter(adjacency))]
    reached: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in reached:
            continue
        reached.add(node_id)
        pending.extend(adjacency[node_id] - reached)
    if reached != set(adjacency):
        raise EvaluationError("used semantic Edges do not form one connected proof graph")


def validate_answer_trace(
    snapshot: GraphSnapshot,
    answer: Mapping[str, Any],
    question: str,
    *,
    disabled_edge_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Independently reconstruct all trace hashes, endpoints, documents and proof IDs."""

    if set(answer) != ANSWER_KEYS:
        raise EvaluationError(
            f"answer fields mismatch: expected {sorted(ANSWER_KEYS)}, got {sorted(answer)}"
        )
    _reject_evaluator_keys(answer)
    expected_question_hash = question_hash(question)
    if answer.get("question_hash") != expected_question_hash:
        raise EvaluationError("answer question_hash mismatch")
    if answer.get("decision") not in {"ACCEPTED", "HOLD"}:
        raise EvaluationError("answer decision must be ACCEPTED or HOLD")
    trace = answer.get("trace")
    if not isinstance(trace, dict) or set(trace) != TRACE_KEYS:
        raise EvaluationError("answer trace fields mismatch")
    if trace.get("question_hash") != expected_question_hash:
        raise EvaluationError("trace question_hash mismatch")
    if trace.get("graph_snapshot_id") != snapshot.graph_snapshot_id:
        raise EvaluationError("trace graph_snapshot_id mismatch")
    if trace.get("decision") != answer.get("decision"):
        raise EvaluationError("trace decision mismatch")

    disabled = sorted(set(disabled_edge_ids))
    if trace.get("disabled_edge_ids") != disabled:
        raise EvaluationError("trace disabled_edge_ids mismatch")
    unknown_disabled = sorted(set(disabled) - set(snapshot.edges))
    if unknown_disabled:
        raise EvaluationError(f"trace disables unknown Edges: {unknown_disabled}")
    run_payload = {
        "graph_snapshot_id": snapshot.graph_snapshot_id,
        "question_hash": expected_question_hash,
        "disabled_edge_ids": disabled,
    }
    expected_run_id = RUN_PREFIX + sha256_value(run_payload)[:32]
    if trace.get("run_id") != expected_run_id:
        raise EvaluationError("trace run_id mismatch")

    visited_edge_ids = trace.get("visited_edge_ids")
    used_edge_ids = trace.get("used_semantic_edge_ids")
    visited_node_ids = trace.get("visited_node_ids")
    if not all(isinstance(value, list) for value in (
        visited_edge_ids, used_edge_ids, visited_node_ids
    )):
        raise EvaluationError("trace visited/used identifiers must be arrays")
    if len(visited_edge_ids) != len(set(visited_edge_ids)):
        raise EvaluationError("trace contains duplicate visited Edge IDs")
    if len(used_edge_ids) != len(set(used_edge_ids)):
        raise EvaluationError("trace contains duplicate used Edge IDs")
    if len(visited_node_ids) != len(set(visited_node_ids)):
        raise EvaluationError("trace contains duplicate visited Node IDs")
    unknown_edges = sorted(set(visited_edge_ids) - set(snapshot.edges))
    unknown_nodes = sorted(set(visited_node_ids) - set(snapshot.nodes))
    if unknown_edges or unknown_nodes:
        raise EvaluationError(
            f"trace references unknown graph records: edges={unknown_edges}, "
            f"nodes={unknown_nodes}"
        )
    if not set(used_edge_ids).issubset(visited_edge_ids):
        raise EvaluationError("used semantic Edges are not a subset of visited Edges")
    if set(used_edge_ids) & set(disabled):
        raise EvaluationError("disabled Edge appears in used semantic Edges")
    for edge_id in used_edge_ids:
        edge = snapshot.edges[edge_id]
        if edge.relation_class != "semantic" or edge.status != "verified":
            raise EvaluationError(f"used Edge is not verified semantic: {edge_id}")
        if not {edge.from_node_id, edge.to_node_id}.issubset(visited_node_ids):
            raise EvaluationError(f"used Edge endpoints were not visited: {edge_id}")
    if answer.get("decision") == "ACCEPTED":
        _assert_used_graph_connected(snapshot, used_edge_ids)

    expected_edge_hashes = [snapshot.edges[item].record_sha256 for item in visited_edge_ids]
    if trace.get("visited_edge_hashes") != expected_edge_hashes:
        raise EvaluationError("trace visited Edge hashes mismatch")
    expected_node_hashes = sorted(
        snapshot.nodes[item].record_sha256 for item in visited_node_ids
    )
    if trace.get("visited_node_hashes") != expected_node_hashes:
        raise EvaluationError("trace visited Node hashes mismatch")
    if trace.get("used_semantic_edge_count") != len(used_edge_ids):
        raise EvaluationError("trace used semantic Edge count mismatch")
    expected_statuses = sorted({snapshot.edges[item].status for item in used_edge_ids})
    if trace.get("used_edge_statuses") != expected_statuses:
        raise EvaluationError("trace used Edge statuses mismatch")

    expected_references = _expected_source_references(snapshot, used_edge_ids)
    if trace.get("resolved_source_references") != expected_references:
        raise EvaluationError("trace resolved source references mismatch")
    expected_documents = sorted({item["path"] for item in expected_references})
    if trace.get("visited_document_paths") != expected_documents:
        raise EvaluationError("trace visited document paths mismatch")
    if trace.get("outbound_network_attempt_count") != 0:
        raise EvaluationError("answerer reported an outbound network attempt")
    elapsed_ms = trace.get("elapsed_ms")
    peak_rss = trace.get("peak_rss_bytes")
    if not isinstance(elapsed_ms, (int, float)) or isinstance(elapsed_ms, bool) or elapsed_ms < 0:
        raise EvaluationError("trace elapsed_ms must be a non-negative number")
    if type(peak_rss) is not int or peak_rss < 0:
        raise EvaluationError("trace peak_rss_bytes must be a non-negative integer")

    facts = answer.get("asserted_facts")
    relations = answer.get("asserted_relations")
    if not isinstance(facts, list) or not isinstance(relations, list):
        raise EvaluationError("asserted facts and relations must be arrays")
    for label, assertions in (("fact", facts), ("relation", relations)):
        for index, assertion in enumerate(assertions):
            if not isinstance(assertion, dict):
                raise EvaluationError(f"{label} assertion {index} must be an object")
            proof_ids = assertion.get("proof_edge_ids")
            if not isinstance(proof_ids, list) or not proof_ids:
                raise EvaluationError(f"{label} assertion {index} has no proof Edges")
            if len(proof_ids) != len(set(proof_ids)):
                raise EvaluationError(f"{label} assertion {index} repeats proof Edges")
            if not set(proof_ids).issubset(used_edge_ids):
                raise EvaluationError(
                    f"{label} assertion {index} uses an untraversed proof Edge"
                )
            if label == "fact":
                field = assertion.get("field")
                if not isinstance(field, str) or not any(
                    _edge_proves_fact(
                        snapshot, snapshot.edges[edge_id], field, assertion.get("value")
                    )
                    for edge_id in proof_ids
                ):
                    raise EvaluationError(
                        f"fact assertion {index} is not supported by its proof Edges"
                    )
            else:
                asserted_tuple = (
                    assertion.get("from"),
                    assertion.get("relation"),
                    assertion.get("to"),
                )
                if not any(
                    (
                        snapshot.nodes[snapshot.edges[edge_id].from_node_id].canonical_key,
                        snapshot.edges[edge_id].relation_type,
                        snapshot.nodes[snapshot.edges[edge_id].to_node_id].canonical_key,
                    )
                    == asserted_tuple
                    for edge_id in proof_ids
                ):
                    raise EvaluationError(
                        f"relation assertion {index} is not supported by its proof Edges"
                    )
    if answer.get("decision") == "HOLD" and (facts or relations):
        raise EvaluationError("HOLD answer must not contain assertions")

    return {
        "used_edge_ids": list(used_edge_ids),
        "visited_document_paths": expected_documents,
        "outbound_network_attempt_count": 0,
    }


def _assert_expected_facts(answer: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    actual: dict[str, Any] = {}
    for fact in answer["asserted_facts"]:
        field = fact.get("field")
        if not isinstance(field, str) or field in actual:
            raise EvaluationError(f"asserted fact field is invalid or duplicated: {field}")
        actual[field] = fact.get("value")
    required = expected.get("required_facts", [])
    if not isinstance(required, list):
        raise EvaluationError("required_facts must be an array")
    for item in required:
        if not isinstance(item, dict) or set(item) != {"field", "value"}:
            raise EvaluationError("invalid required fact contract")
        if actual.get(item["field"]) != item["value"]:
            raise EvaluationError(
                f"required fact mismatch for {item['field']}: "
                f"expected {item['value']!r}, got {actual.get(item['field'])!r}"
            )

    actual_relations = {
        (item.get("from"), item.get("relation"), item.get("to"))
        for item in answer["asserted_relations"]
    }
    for item in expected.get("required_relations", []):
        required_tuple = (item.get("from"), item.get("relation"), item.get("to"))
        if required_tuple not in actual_relations:
            raise EvaluationError(f"required asserted relation is missing: {required_tuple}")

    forbidden_values = expected.get("forbidden_asserted_values", [])
    asserted_text = canonical_json({
        "answer_text": answer.get("answer_text"),
        "asserted_facts": answer["asserted_facts"],
        "asserted_relations": answer["asserted_relations"],
    })
    for value in forbidden_values:
        if isinstance(value, str) and value in asserted_text:
            raise EvaluationError(f"forbidden asserted value found: {value}")


def validate_normal_case(
    snapshot: GraphSnapshot,
    answer: Mapping[str, Any],
    qa_case: Mapping[str, Any],
    gold_mapping: Mapping[str, str],
) -> dict[str, Any]:
    question = qa_case.get("question")
    if not isinstance(question, str):
        raise EvaluationError("QA question must be a string")
    trace_result = validate_answer_trace(snapshot, answer, question)
    expected = qa_case.get("expected")
    requirements = qa_case.get("graph_requirements")
    if not isinstance(expected, dict) or not isinstance(requirements, dict):
        raise EvaluationError("QA expected and graph_requirements must be objects")
    if answer.get("decision") != expected.get("decision"):
        raise EvaluationError(
            f"decision mismatch: expected {expected.get('decision')}, "
            f"got {answer.get('decision')}"
        )

    used = set(trace_result["used_edge_ids"])
    required_gold_keys = requirements.get("required_gold_edge_keys")
    if not isinstance(required_gold_keys, list):
        raise EvaluationError("required_gold_edge_keys must be an array")
    unknown_gold = sorted(set(required_gold_keys) - set(gold_mapping))
    if unknown_gold:
        raise EvaluationError(f"QA references unknown gold Edges: {unknown_gold}")
    required_runtime = {gold_mapping[item] for item in required_gold_keys}
    missing_runtime = sorted(required_runtime - used)
    if missing_runtime:
        raise EvaluationError(f"required semantic Edges were not used: {missing_runtime}")
    minimum_edges = requirements.get("minimum_verified_semantic_edges")
    if not isinstance(minimum_edges, int) or len(used) < minimum_edges:
        raise EvaluationError("minimum verified semantic Edge count was not met")

    visited_documents = set(trace_result["visited_document_paths"])
    required_documents = requirements.get("required_visited_documents")
    if not isinstance(required_documents, list):
        raise EvaluationError("required_visited_documents must be an array")
    missing_documents = sorted(set(required_documents) - visited_documents)
    if missing_documents:
        raise EvaluationError(f"required documents were not visited: {missing_documents}")
    minimum_documents = requirements.get("minimum_distinct_visited_documents")
    if not isinstance(minimum_documents, int) or len(visited_documents) < minimum_documents:
        raise EvaluationError("minimum distinct visited document count was not met")
    if requirements.get("cross_document") is True and len(visited_documents) < 2:
        raise EvaluationError("cross-document answer visited fewer than two documents")

    if answer["decision"] == "ACCEPTED":
        _assert_expected_facts(answer, expected)
    else:
        if answer.get("reason_code") != expected.get("reason_code"):
            raise EvaluationError("HOLD reason_code mismatch")
        concept = expected.get("must_request_concept")
        if answer.get("must_request_concept") != concept:
            raise EvaluationError("HOLD requested concept mismatch")
        if isinstance(concept, str) and concept not in str(answer.get("answer_text", "")):
            raise EvaluationError("HOLD answer text does not request the required concept")
        forbidden = expected.get("must_not_assert_as_final_answer", [])
        assertion_text = canonical_json({
            "answer_text": answer.get("answer_text"),
            "asserted_facts": answer["asserted_facts"],
            "asserted_relations": answer["asserted_relations"],
        })
        for value in forbidden:
            if isinstance(value, str) and value in assertion_text:
                raise EvaluationError(f"HOLD asserted forbidden final value: {value}")
    return {
        "required_runtime_edge_ids": sorted(required_runtime),
        "used_semantic_edge_count": len(used),
        "visited_document_count": len(visited_documents),
    }


def validate_ablation_case(
    snapshot: GraphSnapshot,
    answer: Mapping[str, Any],
    question: str,
) -> dict[str, Any]:
    trace_result = validate_answer_trace(snapshot, answer, question)
    if answer.get("decision") != "HOLD":
        raise EvaluationError("required-Edge ablation did not fail closed to HOLD")
    if answer.get("asserted_facts") or answer.get("asserted_relations"):
        raise EvaluationError("required-Edge ablation retained assertions")
    return trace_result


def _question_envelope(question: str) -> dict[str, str]:
    envelope = {"question": question}
    if set(envelope) != {"question"}:  # defensive invariant visible to tests/audits
        raise EvaluationError("answerer envelope contains evaluator-only fields")
    return envelope


def invoke_answerer(
    *,
    python: Path,
    answerer: Path,
    graph_path: Path,
    question: str,
    io_dir: Path,
    sequence: int,
    dataset: Path,
    guard: NetworkGuard,
) -> tuple[dict[str, Any], GuardedRun]:
    if not answerer.is_file():
        raise EvaluationError(f"answerer is missing: {answerer}")
    input_path = io_dir / f"question-{sequence:04d}.jsonl"
    output_path = io_dir / f"answer-{sequence:04d}.jsonl"
    _write_jsonl(input_path, [_question_envelope(question)])
    argv = [
        str(python.resolve()),
        str(answerer.resolve()),
        "--graph",
        str(graph_path.resolve()),
        "--questions",
        str(input_path.resolve()),
        "--out",
        str(output_path.resolve()),
    ]
    _ensure_runtime_boundary(argv, dataset=dataset, allowed_question_file=input_path)
    run = guard.run(argv, cwd=io_dir)
    records = _read_jsonl(output_path)
    if len(records) != 1:
        raise EvaluationError("answerer must return exactly one record per invocation")
    return records[0], run


def _assert_graph_still_frozen(build: FrozenBuild) -> None:
    if sha256_file(build.graph_path) != build.graph_file_sha256:
        raise EvaluationError("frozen graph file changed during evaluation")
    current = GraphSnapshot.load(build.graph_path)
    if current.graph_snapshot_id != build.snapshot.graph_snapshot_id:
        raise EvaluationError("frozen logical graph changed during evaluation")


@dataclass(frozen=True)
class AblatedSnapshot:
    graph_path: Path
    snapshot: GraphSnapshot
    graph_file_sha256: str
    removed_edge_id: str


def materialize_ablation_snapshot(
    *,
    source_build: FrozenBuild,
    removed_edge_id: str,
    destination: Path,
) -> AblatedSnapshot:
    """Create a new hash-valid SQLite graph with exactly one Edge removed."""

    if removed_edge_id not in source_build.snapshot.edges:
        raise EvaluationError(f"cannot ablate unknown Edge: {removed_edge_id}")
    if destination.exists():
        raise EvaluationError(f"refusing to overwrite ablation snapshot: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_graph_still_frozen(source_build)
    temporary = destination.with_name(f".{destination.name}.building")
    if temporary.exists():
        raise EvaluationError(f"stale ablation staging file exists: {temporary}")
    try:
        shutil.copyfile(source_build.graph_path, temporary)
        temporary.chmod(0o600)
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM edge_evidence WHERE edge_id = ?", (removed_edge_id,)
            )
            deleted = connection.execute(
                "DELETE FROM edges WHERE edge_id = ?", (removed_edge_id,)
            )
            if deleted.rowcount != 1:
                raise EvaluationError(
                    f"ablation did not remove exactly one Edge: {removed_edge_id}"
                )
            logical_payload = {
                "evidence_record_sha256": sorted(
                    row[0]
                    for row in connection.execute(
                        "SELECT record_sha256 FROM source_evidence"
                    )
                ),
                "node_record_sha256": sorted(
                    row[0]
                    for row in connection.execute("SELECT record_sha256 FROM nodes")
                ),
                "edge_record_sha256": sorted(
                    row[0]
                    for row in connection.execute("SELECT record_sha256 FROM edges")
                ),
            }
            logical_sha256 = sha256_value(logical_payload)
            graph_snapshot_id = GRAPH_SNAPSHOT_PREFIX + logical_sha256[:32]
            edge_count = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            replacements = {
                "logical_snapshot_sha256": logical_sha256,
                "graph_snapshot_id": graph_snapshot_id,
                "edge_count": edge_count,
            }
            for key, value in replacements.items():
                updated = connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = ?",
                    (canonical_json(value), key),
                )
                if updated.rowcount != 1:
                    raise EvaluationError(
                        f"ablation snapshot metadata is missing: {key}"
                    )
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise EvaluationError("ablation snapshot failed foreign-key validation")
            connection.commit()
            connection.execute("VACUUM")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

    snapshot = GraphSnapshot.load(destination)
    if removed_edge_id in snapshot.edges:
        raise EvaluationError("ablated Edge remains in published snapshot")
    expected_edge_ids = set(source_build.snapshot.edges) - {removed_edge_id}
    if set(snapshot.edges) != expected_edge_ids:
        raise EvaluationError("ablation changed more than the requested Edge")
    if {
        key: value.record_sha256 for key, value in snapshot.nodes.items()
    } != {
        key: value.record_sha256 for key, value in source_build.snapshot.nodes.items()
    }:
        raise EvaluationError("ablation changed Node records")
    if {
        key: value.record_sha256 for key, value in snapshot.evidence.items()
    } != {
        key: value.record_sha256 for key, value in source_build.snapshot.evidence.items()
    }:
        raise EvaluationError("ablation changed source Evidence records")
    if snapshot.graph_snapshot_id == source_build.snapshot.graph_snapshot_id:
        raise EvaluationError("ablation did not produce a new graph snapshot identity")
    graph_file_sha256 = sha256_file(destination)
    destination.chmod(0o444)
    _assert_graph_still_frozen(source_build)
    return AblatedSnapshot(
        graph_path=destination,
        snapshot=snapshot,
        graph_file_sha256=graph_file_sha256,
        removed_edge_id=removed_edge_id,
    )


def semantic_answer_projection(answer: Mapping[str, Any]) -> dict[str, Any]:
    """Return answer semantics while excluding snapshot-specific trace identity."""

    return {
        key: answer.get(key)
        for key in (
            "question_hash",
            "operation",
            "decision",
            "reason_code",
            "must_request_concept",
            "answer_text",
            "asserted_facts",
            "asserted_relations",
        )
    }


def run_evaluation(
    *,
    dataset: Path,
    phase1_dir: Path,
    output: Path,
    builder: Path,
    answerer: Path,
    python: Path,
) -> dict[str, Any]:
    dataset = dataset.resolve()
    phase1_dir = phase1_dir.resolve()
    output = output.resolve()
    if output.exists():
        raise EvaluationError(f"refusing to overwrite output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent))
    try:
        guard = NetworkGuard(staging)

        # SECURITY BOUNDARY: no evaluator-only file is opened above this line.
        build = build_and_freeze(
            phase1_dir=phase1_dir,
            dataset=dataset,
            builder=builder,
            python=python,
            staging=staging,
            guard=guard,
        )

        # Gold and questions become visible to this evaluator only after freeze.
        gold_edges = _read_jsonl(dataset / "gold" / "expected-graph.jsonl")
        qa_cases = _read_jsonl(dataset / "gold" / "qa-cases.jsonl")
        gold_mapping = match_gold_edges(build.snapshot, gold_edges)

        qa_by_hash: dict[str, dict[str, Any]] = {}
        for qa_case in qa_cases:
            question = qa_case.get("question")
            if not isinstance(question, str):
                raise EvaluationError("QA question must be a string")
            key = question_hash(question)
            if key in qa_by_hash:
                raise EvaluationError(f"duplicate QA question_hash: {key}")
            qa_by_hash[key] = qa_case

        io_dir = staging / "answerer-io"
        io_dir.mkdir()
        normal_answers: dict[str, dict[str, Any]] = {}
        subprocess_runs: list[GuardedRun] = [build.builder_run]
        sequence = 0
        for qa_case in qa_cases:
            sequence += 1
            answer, run = invoke_answerer(
                python=python,
                answerer=answerer,
                graph_path=build.graph_path,
                question=qa_case["question"],
                io_dir=io_dir,
                sequence=sequence,
                dataset=dataset,
                guard=guard,
            )
            subprocess_runs.append(run)
            key = answer.get("question_hash")
            if key not in qa_by_hash:
                raise EvaluationError(f"answer has unknown question_hash: {key}")
            if key in normal_answers:
                raise EvaluationError(f"duplicate normal answer question_hash: {key}")
            normal_answers[key] = answer
        if set(normal_answers) != set(qa_by_hash):
            raise EvaluationError("normal answers do not cover every QA question_hash")

        result_records: list[dict[str, Any]] = []
        accepted_cases: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for key in sorted(qa_by_hash):
            qa_case = qa_by_hash[key]
            answer = normal_answers[key]
            checks = validate_normal_case(
                build.snapshot, answer, qa_case, gold_mapping
            )
            result_records.append({
                "schema_version": SCHEMA_VERSION,
                "record_type": "cross_format_kg_phase2_case_result",
                "run_kind": "normal",
                "qa_case_id": qa_case["qa_case_id"],
                "question_hash": key,
                "decision": answer["decision"],
                "passed": True,
                "checks": checks,
                "answer": answer,
            })
            if answer["decision"] == "ACCEPTED":
                accepted_cases.append((qa_case, answer))

        runtime_snapshot_dir = staging / "runtime-snapshots"
        runtime_snapshot_dir.mkdir()
        ablation_count = 0
        for qa_case, normal_answer in accepted_cases:
            requirements = qa_case["graph_requirements"]
            ablation_contract = requirements.get("edge_ablation", {})
            if not isinstance(ablation_contract, dict) or ablation_contract.get("required") is not True:
                raise EvaluationError("ACCEPTED case is missing required Edge ablation")
            for gold_key in requirements["required_gold_edge_keys"]:
                removed_edge_id = gold_mapping[gold_key]
                ablation_number = ablation_count + 1
                ablated = materialize_ablation_snapshot(
                    source_build=build,
                    removed_edge_id=removed_edge_id,
                    destination=(
                        runtime_snapshot_dir
                        / f"{ablation_number:04d}"
                        / "semantic-graph.sqlite3"
                    ),
                )
                sequence += 1
                answer, run = invoke_answerer(
                    python=python,
                    answerer=answerer,
                    graph_path=ablated.graph_path,
                    question=qa_case["question"],
                    io_dir=io_dir,
                    sequence=sequence,
                    dataset=dataset,
                    guard=guard,
                )
                subprocess_runs.append(run)
                if answer.get("question_hash") != question_hash(qa_case["question"]):
                    raise EvaluationError("ablation answer question_hash mismatch")
                validate_ablation_case(
                    ablated.snapshot,
                    answer,
                    qa_case["question"],
                )
                if (
                    answer.get("asserted_facts") == normal_answer.get("asserted_facts")
                    and answer.get("asserted_relations")
                    == normal_answer.get("asserted_relations")
                ):
                    raise EvaluationError("ablation retained the same asserted answer")
                result_records.append({
                    "schema_version": SCHEMA_VERSION,
                    "record_type": "cross_format_kg_phase2_case_result",
                    "run_kind": "required_edge_ablation",
                    "qa_case_id": qa_case["qa_case_id"],
                    "question_hash": answer["question_hash"],
                    "gold_edge_key": gold_key,
                    "removed_runtime_edge_id": removed_edge_id,
                    "ablation_graph": str(
                        ablated.graph_path.relative_to(staging)
                    ),
                    "ablation_graph_snapshot_id": ablated.snapshot.graph_snapshot_id,
                    "ablation_graph_file_sha256": ablated.graph_file_sha256,
                    "decision": answer["decision"],
                    "passed": True,
                    "answer": answer,
                })
                ablation_count += 1
                _assert_graph_still_frozen(build)

        if not accepted_cases:
            raise EvaluationError("unused-Edge negative control needs an ACCEPTED case")
        control_qa, control_normal = accepted_cases[0]
        control_used = set(
            control_normal["trace"]["used_semantic_edge_ids"]
        )
        non_gold_unused = sorted(
            set(build.snapshot.edges)
            - control_used
            - set(gold_mapping.values())
        )
        any_unused = sorted(set(build.snapshot.edges) - control_used)
        control_candidates = non_gold_unused or any_unused
        if not control_candidates:
            raise EvaluationError("no unused Edge is available for negative control")
        control_removed_edge_id = control_candidates[0]
        control_ablation = materialize_ablation_snapshot(
            source_build=build,
            removed_edge_id=control_removed_edge_id,
            destination=(
                runtime_snapshot_dir
                / f"{ablation_count + 1:04d}"
                / "semantic-graph.sqlite3"
            ),
        )
        sequence += 1
        control_answer, control_run = invoke_answerer(
            python=python,
            answerer=answerer,
            graph_path=control_ablation.graph_path,
            question=control_qa["question"],
            io_dir=io_dir,
            sequence=sequence,
            dataset=dataset,
            guard=guard,
        )
        subprocess_runs.append(control_run)
        validate_normal_case(
            control_ablation.snapshot,
            control_answer,
            control_qa,
            gold_mapping,
        )
        if semantic_answer_projection(control_answer) != semantic_answer_projection(
            control_normal
        ):
            raise EvaluationError(
                "unused-Edge ablation changed the semantic answer projection"
            )
        result_records.append({
            "schema_version": SCHEMA_VERSION,
            "record_type": "cross_format_kg_phase2_case_result",
            "run_kind": "unused_edge_ablation_control",
            "qa_case_id": control_qa["qa_case_id"],
            "question_hash": control_answer["question_hash"],
            "removed_runtime_edge_id": control_removed_edge_id,
            "ablation_graph": str(control_ablation.graph_path.relative_to(staging)),
            "ablation_graph_snapshot_id": (
                control_ablation.snapshot.graph_snapshot_id
            ),
            "ablation_graph_file_sha256": control_ablation.graph_file_sha256,
            "decision": control_answer["decision"],
            "semantic_answer_projection_unchanged": True,
            "passed": True,
            "answer": control_answer,
        })
        negative_control_count = 1

        _assert_graph_still_frozen(build)
        measured_network_attempts = sum(
            item.network_attempt_count for item in subprocess_runs
        )
        if measured_network_attempts != 0:
            raise EvaluationError("measured outbound network attempts were not zero")

        results_path = staging / "phase2-results.jsonl"
        report_path = staging / "phase2-report.json"
        _write_jsonl(results_path, result_records)
        report = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "cross_format_kg_phase2_report",
            "dataset_id": dataset.name,
            "decision": "PASS",
            "graph_snapshot_id": build.snapshot.graph_snapshot_id,
            "graph_file_sha256": build.graph_file_sha256,
            "gold_edge_count": len(gold_edges),
            "matched_gold_edge_count": len(gold_mapping),
            "normal_case_count": len(qa_cases),
            "accepted_case_count": len(accepted_cases),
            "hold_case_count": len(qa_cases) - len(accepted_cases),
            "required_edge_ablation_count": ablation_count,
            "unused_edge_negative_control_count": negative_control_count,
            "ablation_strategy": "independent_hash_valid_sqlite_snapshot",
            "runtime_ablation_snapshot_count": (
                ablation_count + negative_control_count
            ),
            "ablation_trace_disabled_edge_ids": [],
            "source_graph_unchanged_after_ablations": True,
            "source_graph_file_sha256_after_ablations": sha256_file(
                build.graph_path
            ),
            "result_record_count": len(result_records),
            "subprocess_count": len(subprocess_runs),
            "measured_outbound_network_attempt_count": measured_network_attempts,
            "reported_outbound_network_attempt_count": sum(
                int(record["answer"]["trace"]["outbound_network_attempt_count"])
                for record in result_records
            ),
            "gold_boundary": {
                "snapshot_frozen_before_gold_load": True,
                "builder_input_basenames": [
                    "semantic-documents.jsonl",
                    "safe-answer-evidence.jsonl",
                ],
                "answerer_payload_keys": ["question"],
                "answerer_ablation_control_input": "graph_snapshot_only",
                "question_to_gold_match": "question_hash_after_response",
                "gold_available_to_builder": False,
                "gold_available_to_answerer": False,
            },
            "artifacts": {
                "freeze": "graph-freeze.json",
                "graph": "semantic-graph.sqlite3",
                "builder_state": "semantic-graph-state.json",
                "results": "phase2-results.jsonl",
                "ablation_graphs": "runtime-snapshots/",
            },
        }
        _write_json(report_path, report)
        output.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(output)
        return report
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    repository = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--phase1-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--builder",
        type=Path,
        default=repository / "scripts" / "build_cross_document_semantic_graph.py",
    )
    parser.add_argument(
        "--answerer",
        type=Path,
        default=repository / "scripts" / "query_cross_document_semantic_graph.py",
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = run_evaluation(
            dataset=args.dataset,
            phase1_dir=args.phase1_dir,
            output=args.out,
            builder=args.builder,
            answerer=args.answerer,
            python=args.python,
        )
    except (EvaluationError, OSError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
