#!/usr/bin/env python3
"""Validate coverage, lineage, IDs, and relation endpoints for semantic bookmarks."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def stable_id(prefix: str, value: object, length: int = 32) -> str:
    return f"{prefix}_{hashlib.sha256(canonical(value)).hexdigest()[:length]}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    root = Path(args.output_dir).resolve(strict=True)
    coverage = json.loads((root / "semantic-coverage.json").read_text(encoding="utf-8"))
    inventory_path = Path(coverage["source_inventory"]).resolve(strict=True)
    source_root = Path(coverage["source_root"]).resolve(strict=True)
    documents = load_jsonl(root / "semantic-documents.jsonl")
    evidence = load_jsonl(root / "semantic-evidence.jsonl")
    relations = load_jsonl(root / "semantic-relations.jsonl")
    nodes = load_jsonl(root / "semantic-nodes.jsonl")
    inventory = load_jsonl(inventory_path)
    inventory_files = {item["relative_path"]: item for item in inventory if item["kind"] == "file"}
    errors: list[str] = []

    def unique(records: list[dict], key: str, label: str) -> dict[str, dict]:
        values = [record.get(key) for record in records]
        if any(not isinstance(value, str) or not value for value in values) or len(values) != len(set(values)):
            errors.append(f"{label}_ids_invalid")
        return {record[key]: record for record in records if isinstance(record.get(key), str)}

    docs = unique(documents, "document_id", "document")
    evs = unique(evidence, "evidence_id", "evidence")
    rels = unique(relations, "relation_id", "relation")
    semantic_nodes = unique(nodes, "node_id", "semantic_node")
    doc_paths = {record["source"]["relative_path"] for record in documents}
    if doc_paths != set(inventory_files):
        errors.append("file_coverage_mismatch")
    if len(documents) != len(inventory_files):
        errors.append("document_count_mismatch")

    has_evidence = Counter()
    project_edges = Counter()
    all_ids = set(docs) | set(evs) | set(semantic_nodes)
    for record in documents:
        relative = record["source"]["relative_path"]
        item = inventory_files.get(relative)
        if item is None:
            continue
        expected_doc = stable_id("doc", {"relative_path": relative, "sha256": item["sha256"]})
        if record["document_id"] != expected_doc:
            errors.append(f"document_id_mismatch:{relative}")
        path = source_root / relative
        if not path.is_file() or path.is_symlink() or path.stat().st_size != item["size_bytes"]:
            errors.append(f"source_binding_failed:{relative}")
        elif sha256_file(path) != item["sha256"]:
            errors.append(f"source_hash_changed:{relative}")
        listed = record.get("evidence_ids", [])
        if len(listed) != len(set(listed)) or any(value not in evs for value in listed):
            errors.append(f"document_evidence_list_invalid:{relative}")

    for record in evidence:
        if record["document_id"] not in docs:
            errors.append(f"evidence_document_missing:{record['evidence_id']}")
            continue
        expected = stable_id("ev", {
            "document_id": record["document_id"], "locator": record["locator"],
            "observed_text": record["observed_text"],
        })
        if record["evidence_id"] != expected:
            errors.append(f"evidence_id_mismatch:{record['evidence_id']}")
        doc = docs[record["document_id"]]
        if record["source"]["relative_path"] != doc["source"]["relative_path"] or record["source"]["sha256"] != doc["source"]["sha256"]:
            errors.append(f"evidence_lineage_mismatch:{record['evidence_id']}")

    for record in relations:
        if record["from_id"] not in all_ids or record["to_id"] not in all_ids:
            errors.append(f"relation_endpoint_missing:{record['relation_id']}")
            continue
        relation_type = record["relation_type"]
        if relation_type == "has_evidence":
            has_evidence[record["to_id"]] += 1
            if record["from_id"] not in docs or record["to_id"] not in evs:
                errors.append(f"has_evidence_type_invalid:{record['relation_id']}")
        elif relation_type == "member_of_project":
            project_edges[record["from_id"]] += 1
            if record["from_id"] not in docs or semantic_nodes[record["to_id"]]["node_type"] != "project":
                errors.append(f"member_of_project_type_invalid:{record['relation_id']}")
        elif relation_type in {"mentions_theme", "mentions_date"}:
            expected_type = "theme" if relation_type == "mentions_theme" else "date"
            if record["from_id"] not in evs or semantic_nodes[record["to_id"]]["node_type"] != expected_type:
                errors.append(f"semantic_relation_type_invalid:{record['relation_id']}")
            if record.get("status") != "verified_lexical_match":
                errors.append(f"semantic_relation_status_invalid:{record['relation_id']}")
        else:
            errors.append(f"unknown_relation_type:{relation_type}")
    for evidence_id in evs:
        if has_evidence[evidence_id] != 1:
            errors.append(f"evidence_parent_count_invalid:{evidence_id}:{has_evidence[evidence_id]}")
    for document_id in docs:
        if project_edges[document_id] != 1:
            errors.append(f"project_edge_count_invalid:{document_id}:{project_edges[document_id]}")

    output_hashes = coverage["outputs"]
    for name, filename in {
        "documents_sha256": "semantic-documents.jsonl", "evidence_sha256": "semantic-evidence.jsonl",
        "relations_sha256": "semantic-relations.jsonl", "nodes_sha256": "semantic-nodes.jsonl",
    }.items():
        if output_hashes[name] != sha256_file(root / filename):
            errors.append(f"output_hash_mismatch:{filename}")
    if coverage["source_inventory_sha256"] != sha256_file(inventory_path):
        errors.append("inventory_hash_mismatch")
    declared = {
        "file_count": len(inventory_files), "document_count": len(documents), "evidence_count": len(evidence),
        "relation_count": len(relations), "semantic_node_count": len(nodes),
    }
    for key, value in declared.items():
        if coverage.get(key) != value:
            errors.append(f"declared_count_mismatch:{key}")
    if coverage["policy"].get("external_network_used") is not False or coverage["policy"].get("llm_used_for_extraction") is not False:
        errors.append("local_deterministic_policy_invalid")
    result = {
        "status": "PASS" if not errors else "FAIL", **declared,
        "status_counts": dict(Counter(record["status"] for record in documents)), "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
