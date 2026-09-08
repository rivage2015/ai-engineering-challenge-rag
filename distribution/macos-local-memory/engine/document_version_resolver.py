#!/usr/bin/env python3
"""Resolve document-version families with fail-closed human review.

This stage consumes the source-bound Path Graph inventory.  It never reads or
modifies source documents.  Filename years and explicit path status markers may
select one active candidate only under a deterministic policy.  Ambiguity is
preserved as ``needs_human_review`` and a human decision is reusable only while
the complete candidate set and selected source hash remain unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"
RESOLVER_VERSION = "0.1.0"
DOCUMENT_SUFFIXES = {
    ".csv", ".doc", ".docx", ".ods", ".pdf", ".ppt", ".pptx",
    ".tsv", ".xls", ".xlsx",
}
YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
VERSION_TOKEN = re.compile(r"(?i)(?:ver(?:sion)?[._ -]*\d+(?:[._-]\d+)*)")
VERSION_VALUE = re.compile(r"(?i)ver(?:sion)?[._ -]*(\d+(?:[._-]\d+)*)")
COPY_TOKEN = re.compile(r"(?:の)?コピー(?:\s*\(\d+\))?|\bcopy(?:\s*\(\d+\))?\b", re.I)
CURRENT_MARKERS = (
    "実際に使うもの", "実際に使う", "現在使用", "現行", "最新版", "最終版", "承認済み",
    "使うファイル", "採用版", "approved", "final", "current",
)
HISTORICAL_MARKERS = (
    "とりあえず倉庫", "旧資料", "旧版", "過去資料", "廃止", "archive", "archived",
)
DRAFT_MARKERS = (
    "下書き", "編集中", "未承認", "たたき台", "draft", "unapproved",
)


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _family_component(value: str) -> str:
    value = YEAR.sub(" ", normalize(value))
    value = VERSION_TOKEN.sub(" ", value)
    value = COPY_TOKEN.sub(" ", value)
    for marker in (*CURRENT_MARKERS, *HISTORICAL_MARKERS, *DRAFT_MARKERS):
        value = value.replace(normalize(marker), " ")
    return re.sub(r"[\s_\-:：()（）\[\]【】]+", "", value)


def family_key(relative_path: str) -> str:
    """Derive a conservative family key from directory and basename.

    Explicit years, version labels, copy suffixes, and lifecycle markers are
    removed.  Other words remain, so unrelated documents are not merged merely
    because they share an extension or a broad word such as ``業務``.
    """
    path = Path(relative_path)
    stem = _family_component(path.stem)
    parent = "/".join(
        _family_component(part) for part in path.parent.parts
        if _family_component(part)
    )
    return f"{parent}\0{stem}\0{path.suffix.casefold()}"


def markers(relative_path: str, values: Iterable[str]) -> list[str]:
    comparable = normalize(relative_path)
    return [marker for marker in values if normalize(marker) in comparable]


def load_inventory(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"inventory_record_not_object:{line_number}")
            records.append(value)
    return records


def load_decisions(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    records = value.get("decisions") if isinstance(value, dict) else None
    if not isinstance(records, list):
        raise ValueError("decision_file_invalid")
    decisions: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("group_id"), str):
            raise ValueError("decision_record_invalid")
        decisions[record["group_id"]] = record
    return decisions


def candidate(record: dict[str, Any]) -> dict[str, Any] | None:
    relative = record.get("relative_path")
    if (
        record.get("kind") != "file"
        or record.get("read_status") != "observed"
        or not isinstance(relative, str)
        or Path(relative).suffix.casefold() not in DOCUMENT_SUFFIXES
        or not isinstance(record.get("sha256"), str)
    ):
        return None
    years = sorted({int(value) for value in YEAR.findall(normalize(relative))})
    versions = [
        tuple(int(part) for part in re.split(r"[._-]", value))
        for value in VERSION_VALUE.findall(normalize(relative))
    ]
    current = markers(relative, CURRENT_MARKERS)
    historical = markers(relative, HISTORICAL_MARKERS)
    drafts = markers(relative, DRAFT_MARKERS)
    if not (years or versions or current or historical or drafts):
        return None
    return {
        "relative_path": relative,
        "source_sha256": record["sha256"],
        "size_bytes": record.get("size_bytes"),
        "mtime_ns": record.get("mtime_ns"),
        "birthtime_ns": record.get("birthtime_ns"),
        "explicit_years": years,
        "explicit_versions": [list(value) for value in versions],
        "current_markers": current,
        "historical_markers": historical,
        "draft_markers": drafts,
    }


def candidate_set_hash(candidates: list[dict[str, Any]]) -> str:
    return sha256_json([
        {
            "relative_path": item["relative_path"],
            "source_sha256": item["source_sha256"],
            "mtime_ns": item["mtime_ns"],
            "birthtime_ns": item["birthtime_ns"],
            "explicit_years": item["explicit_years"],
            "explicit_versions": item["explicit_versions"],
            "current_markers": item["current_markers"],
            "historical_markers": item["historical_markers"],
            "draft_markers": item["draft_markers"],
        }
        for item in candidates
    ])


def automatic_selection(candidates: list[dict[str, Any]]) -> tuple[str | None, str, list[str]]:
    current = [
        item for item in candidates
        if item["current_markers"]
        and not item["draft_markers"]
        and not item["historical_markers"]
    ]
    if len(current) > 1:
        return None, "multiple_current_markers", [item["relative_path"] for item in current]
    if current:
        proposed = current[0]
        dated = [item for item in candidates if item["explicit_years"]]
        if proposed["explicit_years"] and dated:
            maximum_year = max(max(item["explicit_years"]) for item in dated)
            if max(proposed["explicit_years"]) < maximum_year:
                return None, "current_marker_conflicts_with_latest_year", [
                    proposed["relative_path"],
                    *[
                        item["relative_path"] for item in dated
                        if maximum_year in item["explicit_years"]
                    ],
                ]
        return proposed["relative_path"], "unique_explicit_current_marker", []
    if all(item["explicit_years"] for item in candidates):
        maximum_year = max(max(item["explicit_years"]) for item in candidates)
        newest = [item for item in candidates if maximum_year in item["explicit_years"]]
        if len(newest) != 1:
            return None, "latest_year_not_unique", [item["relative_path"] for item in newest]
        proposed = newest[0]
        if proposed["draft_markers"]:
            return None, "latest_year_is_draft", [proposed["relative_path"]]
        if proposed["historical_markers"]:
            return None, "latest_year_marked_historical", [proposed["relative_path"]]
        return proposed["relative_path"], "unique_latest_explicit_year", []
    if all(item["explicit_versions"] for item in candidates):
        maximum_version = max(
            max(tuple(value) for value in item["explicit_versions"])
            for item in candidates
        )
        newest = [
            item for item in candidates
            if maximum_version in {tuple(value) for value in item["explicit_versions"]}
        ]
        if len(newest) == 1 and not (
            newest[0]["draft_markers"] or newest[0]["historical_markers"]
        ):
            return newest[0]["relative_path"], "unique_latest_explicit_version", []
        return None, "latest_version_not_unique_or_not_active", [
            item["relative_path"] for item in newest
        ]
    return None, "insufficient_comparable_version_signals", [
        item["relative_path"] for item in candidates
    ]


def resolve_group(
    key: str,
    candidates: list[dict[str, Any]],
    decision: dict[str, Any] | None,
) -> dict[str, Any]:
    candidates.sort(key=lambda item: item["relative_path"].encode("utf-8"))
    set_hash = candidate_set_hash(candidates)
    group_id = "version_set_" + sha256_json({"family_key": key})[:32]
    selected: str | None = None
    resolution_basis = "automatic"
    reason_code = ""
    conflicts: list[str] = []
    stale_decision_reason: str | None = None
    if decision is not None:
        proposed = decision.get("selected_relative_path")
        selected_candidate = next(
            (item for item in candidates if item["relative_path"] == proposed), None
        )
        if decision.get("candidate_set_sha256") != set_hash:
            stale_decision_reason = "candidate_set_changed"
        elif selected_candidate is None:
            stale_decision_reason = "selected_candidate_missing"
        elif decision.get("selected_source_sha256") != selected_candidate["source_sha256"]:
            stale_decision_reason = "selected_source_changed"
        else:
            selected = proposed
            resolution_basis = "human"
            reason_code = "human_confirmed_active"
    if selected is None:
        selected, reason_code, conflicts = automatic_selection(candidates)
        if stale_decision_reason is not None:
            selected = None
            reason_code = "stale_human_decision"
            conflicts = [stale_decision_reason]
    status = "resolved" if selected is not None else "needs_human_review"
    return {
        "group_id": group_id,
        "family_key_sha256": sha256_json(key),
        "candidate_set_sha256": set_hash,
        "status": status,
        "selected_relative_path": selected,
        "resolution_basis": resolution_basis if selected else None,
        "reason_code": reason_code,
        "conflicts": conflicts,
        "candidates": [
            {
                **item,
                "disposition": (
                    "active" if item["relative_path"] == selected
                    else "historical" if selected else "needs_human_review"
                ),
            }
            for item in candidates
        ],
    }


def graph_projection(groups: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for group in groups:
        set_node = group["group_id"]
        nodes.append({
            "node_id": set_node,
            "node_type": "document_version_set",
            "status": group["status"],
            "candidate_set_sha256": group["candidate_set_sha256"],
        })
        document_ids: dict[str, str] = {}
        for item in group["candidates"]:
            document_id = "document_version_" + sha256_json({
                "relative_path": item["relative_path"],
                "source_sha256": item["source_sha256"],
            })[:32]
            document_ids[item["relative_path"]] = document_id
            nodes.append({
                "node_id": document_id,
                "node_type": "document_version",
                "status": item["disposition"],
                "relative_path": item["relative_path"],
                "source_sha256": item["source_sha256"],
                "explicit_years": item["explicit_years"],
                "explicit_versions": item["explicit_versions"],
            })
            edges.append({
                "edge_id": "edge_" + sha256_json([document_id, "candidate_for", set_node])[:32],
                "from_node_id": document_id,
                "edge_type": "candidate_for",
                "to_node_id": set_node,
                "basis": "normalized_filename_family",
                "status": "candidate",
            })
        if group["selected_relative_path"]:
            selected_id = document_ids[group["selected_relative_path"]]
            edges.append({
                "edge_id": "edge_" + sha256_json([set_node, "active_version", selected_id])[:32],
                "from_node_id": set_node,
                "edge_type": "active_version",
                "to_node_id": selected_id,
                "basis": group["reason_code"],
                "status": "verified" if group["resolution_basis"] == "human" else "policy_resolved",
            })
    nodes.sort(key=lambda item: item["node_id"])
    edges.sort(key=lambda item: item["edge_id"])
    return nodes, edges


def build(
    inventory_path: Path,
    output_path: Path,
    decisions_path: Path | None = None,
) -> dict[str, Any]:
    records = load_inventory(inventory_path)
    decisions = load_decisions(decisions_path)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        item = candidate(record)
        if item is None:
            continue
        grouped.setdefault(family_key(item["relative_path"]), []).append(item)
    groups = []
    for key, candidates in sorted(grouped.items()):
        if len(candidates) < 2:
            continue
        group_id = "version_set_" + sha256_json({"family_key": key})[:32]
        groups.append(resolve_group(key, candidates, decisions.get(group_id)))
    nodes, edges = graph_projection(groups)
    core = {
        "schema_version": SCHEMA_VERSION,
        "resolver": "document-version-resolver",
        "resolver_version": RESOLVER_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "inventory_path": str(inventory_path.resolve()),
            "inventory_sha256": sha256_file(inventory_path),
            "decisions_path": str(decisions_path.resolve()) if decisions_path else None,
            "decisions_sha256": sha256_file(decisions_path) if decisions_path and decisions_path.exists() else None,
        },
        "policy": {
            "creation_time_is_authoritative": False,
            "modification_time_is_authoritative": False,
            "automatic_rule": "unique_explicit_current_else_comparable_latest_year_or_version",
            "ambiguous_action": "needs_human_review",
            "historical_sources_retained": True,
        },
        "counts": {
            "groups": len(groups),
            "resolved": sum(item["status"] == "resolved" for item in groups),
            "needs_human_review": sum(item["status"] == "needs_human_review" for item in groups),
        },
        "groups": groups,
        "nodes": nodes,
        "edges": edges,
    }
    result = {**core, "graph_sha256": sha256_json(core)}
    atomic_json(output_path, result)
    return result


def validate(graph_path: Path, inventory_path: Path) -> dict[str, Any]:
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if graph.get("source", {}).get("inventory_sha256") != sha256_file(inventory_path):
        errors.append("inventory_changed")
    core = {key: value for key, value in graph.items() if key != "graph_sha256"}
    if graph.get("graph_sha256") != sha256_json(core):
        errors.append("graph_hash_mismatch")
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    node_ids = {item.get("node_id") for item in nodes if isinstance(item, dict)}
    if len(node_ids) != len(nodes):
        errors.append("duplicate_node_id")
    for edge in edges:
        if edge.get("from_node_id") not in node_ids or edge.get("to_node_id") not in node_ids:
            errors.append(f"unknown_edge_endpoint:{edge.get('edge_id')}")
    for group in graph.get("groups", []):
        active = [item for item in group.get("candidates", []) if item.get("disposition") == "active"]
        if group.get("status") == "resolved" and len(active) != 1:
            errors.append(f"resolved_group_active_count:{group.get('group_id')}")
        if group.get("status") == "needs_human_review" and active:
            errors.append(f"review_group_has_active:{group.get('group_id')}")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors}


def record_decision(
    graph_path: Path,
    decisions_path: Path,
    group_id: str,
    selected_relative_path: str,
    actor: str,
) -> dict[str, Any]:
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    core = {key: value for key, value in graph.items() if key != "graph_sha256"}
    if graph.get("graph_sha256") != sha256_json(core):
        raise ValueError("decision_graph_hash_mismatch")
    group = next((item for item in graph.get("groups", []) if item.get("group_id") == group_id), None)
    if group is None:
        raise ValueError("decision_group_missing")
    selected = next(
        (item for item in group["candidates"] if item["relative_path"] == selected_relative_path), None
    )
    if selected is None:
        raise ValueError("decision_candidate_missing")
    existing = load_decisions(decisions_path)
    existing[group_id] = {
        "group_id": group_id,
        "candidate_set_sha256": group["candidate_set_sha256"],
        "selected_relative_path": selected_relative_path,
        "selected_source_sha256": selected["source_sha256"],
        "decided_by": actor,
        "decided_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "decisions": [existing[key] for key in sorted(existing)],
    }
    atomic_json(decisions_path, result)
    return existing[group_id]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--inventory", required=True, type=Path)
    build_parser.add_argument("--output", required=True, type=Path)
    build_parser.add_argument("--decisions", type=Path)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--graph", required=True, type=Path)
    validate_parser.add_argument("--inventory", required=True, type=Path)
    decide_parser = subparsers.add_parser("decide")
    decide_parser.add_argument("--graph", required=True, type=Path)
    decide_parser.add_argument("--decisions", required=True, type=Path)
    decide_parser.add_argument("--group-id", required=True)
    decide_parser.add_argument("--select", required=True)
    decide_parser.add_argument("--actor", default="human")
    args = parser.parse_args()
    if args.command == "build":
        result = build(args.inventory.resolve(strict=True), args.output, args.decisions)
        print(json.dumps(result["counts"], ensure_ascii=False, indent=2))
    elif args.command == "validate":
        result = validate(args.graph.resolve(strict=True), args.inventory.resolve(strict=True))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 1
    else:
        result = record_decision(
            args.graph.resolve(strict=True), args.decisions, args.group_id,
            args.select, args.actor,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
