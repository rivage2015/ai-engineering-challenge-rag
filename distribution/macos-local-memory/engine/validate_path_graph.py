#!/usr/bin/env python3
"""Validate path Evidence Graph coverage, references, hashes, and edge policies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections import Counter
from pathlib import Path


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def validate(graph_path: Path, inventory_path: Path) -> dict:
    errors: list[str] = []
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    inventory_bytes = inventory_path.read_bytes()
    inventory = [json.loads(line) for line in inventory_bytes.splitlines() if line.strip()]
    root = Path(graph["source_universe"]["scope"])
    if graph["integrity"]["source_inventory_sha256"] != sha256_bytes(inventory_bytes):
        fail(errors, "source_inventory_sha256_mismatch")
    graph_for_hash = {**graph, "integrity": {**graph["integrity"], "graph_content_sha256": None}}
    if graph["integrity"]["graph_content_sha256"] != sha256_bytes(canonical(graph_for_hash)):
        fail(errors, "graph_content_sha256_mismatch")

    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    node_by_id = {node["node_id"]: node for node in nodes}
    if len(node_by_id) != len(nodes):
        fail(errors, "duplicate_node_id")
    edge_ids = [edge["edge_id"] for edge in edges]
    if len(set(edge_ids)) != len(edge_ids):
        fail(errors, "duplicate_edge_id")

    root_nodes = [node for node in nodes if node["node_type"] == "filesystem_root"]
    if len(root_nodes) != 1:
        fail(errors, "root_node_count_invalid")
    nonroot = [node for node in nodes if node["node_type"] != "filesystem_root"]
    if len(nonroot) != len(inventory):
        fail(errors, "inventory_node_count_mismatch")
    node_by_path = {node["raw_value"]["relative_path"]: node for node in nonroot}
    if len(node_by_path) != len(nonroot):
        fail(errors, "duplicate_relative_path")
    inventory_by_path = {item["relative_path"]: item for item in inventory}
    if set(node_by_path) != set(inventory_by_path):
        fail(errors, "inventory_path_set_mismatch")

    contains_in = Counter()
    for edge in edges:
        if edge["from_node_id"] not in node_by_id or edge["to_node_id"] not in node_by_id:
            fail(errors, f"unknown_edge_endpoint:{edge['edge_id']}")
            continue
        if edge["edge_type"] == "contains":
            contains_in[edge["to_node_id"]] += 1
            parent = node_by_id[edge["from_node_id"]]
            child = node_by_id[edge["to_node_id"]]
            child_path = child["raw_value"]["relative_path"]
            expected_parent = Path(child_path).parent.as_posix()
            if expected_parent == "":
                expected_parent = "."
            actual_parent = "." if parent["node_type"] == "filesystem_root" else parent["raw_value"]["relative_path"]
            if expected_parent != actual_parent:
                fail(errors, f"contains_parent_mismatch:{edge['edge_id']}")
        elif edge["edge_type"] == "exact_duplicate":
            left = node_by_id[edge["from_node_id"]]
            right = node_by_id[edge["to_node_id"]]
            if left["node_type"] != "filesystem_file" or right["node_type"] != "filesystem_file":
                fail(errors, f"duplicate_edge_nonfile:{edge['edge_id']}")
            if left["raw_value"]["size_bytes"] != right["raw_value"]["size_bytes"]:
                fail(errors, f"duplicate_size_mismatch:{edge['edge_id']}")
            if left["source"]["sha256"] != right["source"]["sha256"]:
                fail(errors, f"duplicate_hash_mismatch:{edge['edge_id']}")
        else:
            fail(errors, f"unknown_edge_type:{edge['edge_type']}")
    for node in nonroot:
        if contains_in[node["node_id"]] != 1:
            fail(errors, f"contains_indegree_invalid:{node['node_id']}:{contains_in[node['node_id']]}")

    for relative, item in inventory_by_path.items():
        path = root / relative
        try:
            metadata = path.lstat()
        except OSError as exc:
            fail(errors, f"current_source_missing:{relative}:{type(exc).__name__}")
            continue
        if item["kind"] == "file":
            if not stat.S_ISREG(metadata.st_mode):
                fail(errors, f"current_source_type_changed:{relative}")
                continue
            if metadata.st_size != item["size_bytes"]:
                fail(errors, f"current_source_size_changed:{relative}")
            elif sha256_file(path) != item["sha256"]:
                fail(errors, f"current_source_hash_changed:{relative}")
        elif item["kind"] == "directory" and not stat.S_ISDIR(metadata.st_mode):
            fail(errors, f"current_source_type_changed:{relative}")
        elif item["kind"] == "symlink" and not stat.S_ISLNK(metadata.st_mode):
            fail(errors, f"current_source_type_changed:{relative}")

    expected_status = "complete" if not errors else "incomplete"
    if graph["coverage_audit"]["status"] != expected_status:
        fail(errors, "declared_coverage_status_mismatch")
    return {
        "status": "PASS" if not errors else "FAIL",
        "node_count": len(nodes),
        "edge_count": len(edges),
        "inventory_count": len(inventory),
        "exact_duplicate_edges": sum(edge["edge_type"] == "exact_duplicate" for edge in edges),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("graph")
    parser.add_argument("inventory")
    args = parser.parse_args()
    result = validate(Path(args.graph).resolve(strict=True), Path(args.inventory).resolve(strict=True))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
