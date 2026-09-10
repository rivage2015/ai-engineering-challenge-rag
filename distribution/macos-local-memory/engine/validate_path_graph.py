#!/usr/bin/env python3
"""Validate path Evidence Graph coverage, references, hashes, and edge policies."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath


# Shared descriptor-bound filesystem primitives, not an independent producer
# oracle. We verify submitted graph/path bindings separately below.
_SPEC = importlib.util.spec_from_file_location(
    "path_validator_source_observer", Path(__file__).with_name("build_path_graph.py")
)
SOURCE_OBSERVER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(SOURCE_OBSERVER)


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def _report(errors: list[str], nodes=(), edges=(), inventory=()) -> dict:
    return {
        "status": "PASS" if not errors else "FAIL",
        "node_count": len(nodes),
        "edge_count": len(edges),
        "inventory_count": len(inventory),
        "exact_duplicate_edges": sum(edge["edge_type"] == "exact_duplicate" for edge in edges),
        "errors": errors,
    }


def _unique_object(pairs: list) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and value != "." and ".." not in path.parts and path.as_posix() == value


def validate(graph_path: Path, inventory_path: Path, *, source_root: Path | None = None) -> dict:
    # The submitted graph must never choose which local directory to read.
    if source_root is None:
        return _report(["source_root_required"])
    try:
        root = Path(source_root)
    except TypeError:
        return _report(["source_root_invalid"])
    if not root.is_absolute() or ".." in root.parts or "\0" in str(root):
        return _report(["source_root_invalid"])
    try:
        return _validate(graph_path, inventory_path, root)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        # No malformed or unreadable input can be promoted to a valid empty
        # graph. Do not expose file contents in the diagnostic.
        return _report([f"path_validation_input_or_source_error:{type(exc).__name__}"])


def _validate(graph_path: Path, inventory_path: Path, root: Path) -> dict:
    errors: list[str] = []
    graph = json.loads(graph_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    inventory_bytes = inventory_path.read_bytes()
    inventory = [json.loads(line, object_pairs_hook=_unique_object) for line in inventory_bytes.splitlines() if line.strip()]
    if graph["source_universe"]["scope"] != str(root):
        return _report(["source_root_scope_mismatch"])
    if graph["schema_version"] != "1.1":
        fail(errors, "path_graph_schema_unsupported")
    if graph["integrity"]["source_inventory_sha256"] != sha256_bytes(inventory_bytes):
        fail(errors, "source_inventory_sha256_mismatch")
    graph_for_hash = {**graph, "integrity": {**graph["integrity"], "graph_content_sha256": None}}
    if graph["integrity"]["graph_content_sha256"] != sha256_bytes(canonical(graph_for_hash)):
        fail(errors, "graph_content_sha256_mismatch")

    inventory_by_path = {}
    for item in inventory:
        relative = item["relative_path"]
        if not _relative_path(relative):
            fail(errors, "inventory_relative_path_invalid")
            continue
        if relative in inventory_by_path:
            fail(errors, "duplicate_inventory_relative_path")
        inventory_by_path[relative] = item
        kind = item["kind"]
        if kind not in {"file", "directory", "symlink", "other"}:
            fail(errors, "inventory_kind_invalid")
        if type(item["size_bytes"]) is not int or item["size_bytes"] < 0 or type(item["mtime_ns"]) is not int:
            fail(errors, "inventory_metadata_invalid")
        if item.get("birthtime_ns") is not None and type(item["birthtime_ns"]) is not int:
            fail(errors, "inventory_birthtime_invalid")
        if item["read_status"] != "observed":
            fail(errors, "inventory_unresolved_source")
        if kind in {"file", "symlink"}:
            if not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                fail(errors, "inventory_source_hash_invalid")
        elif item["sha256"] is not None:
            fail(errors, "inventory_nonfile_hash_invalid")
    if errors:
        return _report(errors)

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
    for node in nodes:
        if not isinstance(node["node_id"], str) or not node["node_id"]:
            fail(errors, "node_id_invalid")
        if node["status"] != "observed":
            fail(errors, "node_not_observed")
    nonroot = [node for node in nodes if node["node_type"] != "filesystem_root"]
    if len(nonroot) != len(inventory):
        fail(errors, "inventory_node_count_mismatch")
    node_by_path = {node["raw_value"]["relative_path"]: node for node in nonroot}
    if len(node_by_path) != len(nonroot):
        fail(errors, "duplicate_relative_path")
    if any(not _relative_path(relative) for relative in node_by_path):
        fail(errors, "node_relative_path_invalid")
    if set(node_by_path) != set(inventory_by_path):
        fail(errors, "inventory_path_set_mismatch")

    if len(root_nodes) == 1:
        root_node = root_nodes[0]
        if root_node["raw_value"]["absolute_path"] != str(root) or root_node["source"]["path"] != str(root) or root_node["source"]["locator"]["relative_path"] != ".":
            fail(errors, "root_node_source_binding_mismatch")
    for relative, node in node_by_path.items():
        if node["source"]["path"] != str(root / relative) or node["source"]["locator"]["relative_path"] != relative:
            fail(errors, "node_source_path_binding_mismatch")

    contains_in = Counter()
    for edge in edges:
        if edge["scope"]["root"] != str(root):
            fail(errors, "edge_scope_mismatch")
        if edge["from_node_id"] not in node_by_id or edge["to_node_id"] not in node_by_id:
            fail(errors, f"unknown_edge_endpoint:{edge['edge_id']}")
            continue
        if edge["edge_type"] == "contains":
            contains_in[edge["to_node_id"]] += 1
            parent = node_by_id[edge["from_node_id"]]
            child = node_by_id[edge["to_node_id"]]
            if parent["node_type"] not in {"filesystem_root", "filesystem_directory"}:
                fail(errors, "contains_parent_not_directory")
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

    if graph["coverage_audit"]["status"] != "complete" or graph["source_universe"]["coverage_status"] != "complete" or graph["unresolved"] or graph["coverage_audit"]["missing_items"]:
        fail(errors, "declared_coverage_status_mismatch")
    if errors:
        return _report(errors, nodes, edges, inventory)

    # Only validated relative paths and the externally supplied root reach
    # this point. Traverse the current complete path set without following
    # symlinks; never reopen a graph-provided full source pathname.
    current_entries, observation_errors = SOURCE_OBSERVER.enumerate_entries(root)
    if observation_errors:
        fail(errors, "current_source_observation_incomplete")
        return _report(errors, nodes, edges, inventory)
    current_by_path = {item["relative_path"]: item for item in current_entries}
    if set(current_by_path) != set(inventory_by_path):
        fail(errors, "current_source_path_set_changed")
        return _report(errors, nodes, edges, inventory)
    dir_hashes = SOURCE_OBSERVER.directory_hashes(current_entries)
    if root_nodes[0]["source"]["sha256"] != dir_hashes["."]:
        fail(errors, "root_source_hash_changed")
    for relative, item in inventory_by_path.items():
        observed = current_by_path[relative]
        for key in ("kind", "size_bytes", "mtime_ns", "sha256", "read_status"):
            if item[key] != observed[key]:
                fail(errors, f"current_source_{key}_changed:{relative}")
        if "birthtime_ns" in item and item["birthtime_ns"] != observed["birthtime_ns"]:
            fail(errors, f"current_source_birthtime_changed:{relative}")
        node = node_by_path[relative]
        if node["node_type"] != "filesystem_" + observed["kind"]:
            fail(errors, f"node_source_type_mismatch:{relative}")
        for key in ("relative_path", "basename", "kind", "extension", "size_bytes", "mtime_ns", "birthtime_ns", "mode", "symlink_target"):
            if node["raw_value"].get(key) != observed[key]:
                fail(errors, f"node_source_{key}_mismatch:{relative}")
        digest = dir_hashes[relative] if observed["kind"] == "directory" else observed["sha256"]
        if node["source"]["sha256"] != digest:
            fail(errors, f"node_source_hash_mismatch:{relative}")
    return _report(errors, nodes, edges, inventory)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("graph")
    parser.add_argument("inventory")
    parser.add_argument("--source-root", required=True)
    args = parser.parse_args()
    result = validate(Path(args.graph), Path(args.inventory), source_root=Path(args.source_root))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
