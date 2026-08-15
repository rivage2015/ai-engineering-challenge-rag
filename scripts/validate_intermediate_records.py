#!/usr/bin/env python3
"""Validate local intermediate JSONL invariants without external packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from probe_intermediate_records import normalize_text


PATTERNS = {
    "document": re.compile(r"^doc_[0-9a-f]{16,64}$"),
    "evidence": re.compile(r"^ev_[0-9a-f]{16,64}$"),
    "relation": re.compile(r"^rel_[0-9a-f]{16,64}$"),
}
REQUIRED = {
    "document": {"schema_version", "record_type", "document_id", "source", "extraction"},
    "evidence": {"schema_version", "record_type", "evidence_id", "document_id", "evidence_type", "location", "content", "provenance"},
    "relation": {"schema_version", "record_type", "relation_id", "relation_class", "relation_type", "from_ref", "to_ref", "provenance", "status"},
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{digest_value(value)[:32]}"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return records


def content_hash_payload(item: dict[str, Any]) -> dict[str, Any]:
    for key in ("raw_text", "raw_value", "content_ref"):
        if key in item:
            return {key: item[key]}
    raise ValueError("content has none of raw_text/raw_value/content_ref")


def validate(directory: Path, source_root: Path | None = None) -> dict[str, int]:
    groups = {
        "document": read_jsonl(directory / "documents.jsonl"),
        "evidence": read_jsonl(directory / "evidence.jsonl"),
        "relation": read_jsonl(directory / "relations.jsonl"),
    }
    errors: list[str] = []
    ids: dict[str, set[str]] = {key: set() for key in groups}
    for kind, records in groups.items():
        id_key = f"{kind}_id"
        for index, record in enumerate(records, 1):
            label = f"{kind}[{index}]"
            missing = REQUIRED[kind] - record.keys()
            if missing:
                errors.append(f"{label}: missing {sorted(missing)}")
            if record.get("schema_version") != "0.1" or record.get("record_type") != kind:
                errors.append(f"{label}: schema_version/record_type mismatch")
            record_id = record.get(id_key, "")
            if not PATTERNS[kind].fullmatch(record_id):
                errors.append(f"{label}: malformed {id_key}: {record_id!r}")
            if record_id in ids[kind]:
                errors.append(f"{label}: duplicate id {record_id}")
            ids[kind].add(record_id)

    evidence_by_id = {item["evidence_id"]: item for item in groups["evidence"] if "evidence_id" in item}
    for item in groups["document"]:
        source = item.get("source", {})
        expected = stable_id("doc", {
            "relative_path": source.get("relative_path"),
            "source_sha256": source.get("sha256"),
        })
        if item.get("document_id") != expected:
            errors.append(f"{item.get('document_id', '<missing>')}: unstable document id")
        if source_root is not None:
            root = source_root.resolve()
            source_path = (root / source.get("relative_path", "")).resolve()
            try:
                source_path.relative_to(root)
            except ValueError:
                errors.append(f"{item.get('document_id', '<missing>')}: source path escapes root")
            else:
                if not source_path.is_file():
                    errors.append(f"{item.get('document_id', '<missing>')}: source file is missing")
                else:
                    source_bytes = source_path.read_bytes()
                    actual_source_sha = hashlib.sha256(source_bytes).hexdigest()
                    if actual_source_sha != source.get("sha256"):
                        errors.append(f"{item.get('document_id', '<missing>')}: source hash mismatch")
                    if len(source_bytes) != source.get("size_bytes"):
                        errors.append(f"{item.get('document_id', '<missing>')}: source size mismatch")
    for item in groups["evidence"]:
        ev_id = item.get("evidence_id", "<missing>")
        doc_id = item.get("document_id")
        if doc_id not in ids["document"]:
            errors.append(f"{ev_id}: dangling document_id {doc_id}")
        parent_id = item.get("parent_evidence_id")
        if parent_id:
            parent = evidence_by_id.get(parent_id)
            if parent is None:
                errors.append(f"{ev_id}: dangling parent {parent_id}")
            elif parent.get("document_id") != doc_id:
                errors.append(f"{ev_id}: parent belongs to another document")
        item_content = item.get("content", {})
        try:
            actual = digest_value(content_hash_payload(item_content))
            if actual != item_content.get("sha256"):
                errors.append(f"{ev_id}: content hash mismatch")
            expected = stable_id("ev", {
                "document_id": doc_id,
                "evidence_type": item.get("evidence_type"),
                "location": item.get("location"),
                "content_sha256": item_content.get("sha256"),
            })
            if ev_id != expected:
                errors.append(f"{ev_id}: unstable evidence id")
            if "raw_text" in item_content:
                expected_normalized = normalize_text(item_content["raw_text"])
                if item_content.get("normalized_text") != expected_normalized:
                    errors.append(f"{ev_id}: normalized_text is missing or inconsistent")
            if "raw_value" in item_content and item_content.get("normalized_value") != item_content["raw_value"]:
                errors.append(f"{ev_id}: normalized_value is missing or inconsistent")
        except ValueError as exc:
            errors.append(f"{ev_id}: {exc}")

    for relation in groups["relation"]:
        rel_id = relation.get("relation_id", "<missing>")
        for side in ("from_ref", "to_ref"):
            ref = relation.get(side, {})
            kind = ref.get("record_type")
            record_id = ref.get("record_id")
            if kind not in ("document", "evidence"):
                errors.append(f"{rel_id}: invalid {side} type {kind!r}")
            elif record_id not in ids[kind]:
                errors.append(f"{rel_id}: dangling {side} {record_id}")
        for evidence_id in relation.get("supporting_evidence_ids", []):
            if evidence_id not in ids["evidence"]:
                errors.append(f"{rel_id}: dangling supporting evidence {evidence_id}")
        expected = stable_id("rel", {
            "class": relation.get("relation_class"),
            "type": relation.get("relation_type"),
            "from": relation.get("from_ref"),
            "to": relation.get("to_ref"),
            "generator": relation.get("provenance", {}).get("generated_by"),
            "generator_version": relation.get("provenance", {}).get("generator_version"),
        })
        if rel_id != expected:
            errors.append(f"{rel_id}: unstable relation id")

    if errors:
        raise ValueError("validation failed:\n- " + "\n- ".join(errors))
    return {kind: len(records) for kind, records in groups.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--root", type=Path, help="optionally recheck source file size and SHA-256")
    args = parser.parse_args()
    print(canonical_json({"status": "ok", "counts": validate(args.directory, args.root)}))


if __name__ == "__main__":
    main()
