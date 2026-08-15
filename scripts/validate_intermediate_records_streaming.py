#!/usr/bin/env python3
"""Validate large intermediate JSONL outputs with a disk-backed ID registry."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterator

from lexical_search_common import canonical_json, digest_file
from probe_intermediate_records import digest_value, normalize_text, stable_id


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


def records(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield line_number, json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc


def content_hash_payload(item: dict[str, Any]) -> dict[str, Any]:
    for key in ("raw_text", "raw_value", "content_ref"):
        if key in item:
            return {key: item[key]}
    raise ValueError("content has none of raw_text/raw_value/content_ref")


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=FILE;
        CREATE TABLE documents(id TEXT PRIMARY KEY, relative_path TEXT NOT NULL);
        CREATE TABLE evidence(id TEXT PRIMARY KEY, document_id TEXT NOT NULL, parent_id TEXT);
        CREATE INDEX evidence_document_idx ON evidence(document_id);
        CREATE INDEX evidence_parent_idx ON evidence(parent_id);
        CREATE TABLE relations(id TEXT PRIMARY KEY);
        CREATE TABLE refs(relation_id TEXT NOT NULL, side TEXT NOT NULL, kind TEXT NOT NULL, record_id TEXT NOT NULL);
        CREATE INDEX refs_kind_record_idx ON refs(kind, record_id);
        CREATE TABLE supporting(relation_id TEXT NOT NULL, evidence_id TEXT NOT NULL);
        CREATE INDEX supporting_evidence_idx ON supporting(evidence_id);
    """)


def validate(directory: Path, source_root: Path | None = None) -> dict[str, int]:
    errors: list[str] = []
    counts = {"document": 0, "evidence": 0, "relation": 0}
    with tempfile.TemporaryDirectory(prefix="aiec-intermediate-validation-") as temporary:
        connection = sqlite3.connect(Path(temporary) / "ids.sqlite3")
        initialize(connection)

        for line_number, record in records(directory / "documents.jsonl"):
            counts["document"] += 1
            label = f"document[{line_number}]"
            missing = REQUIRED["document"] - record.keys()
            if missing:
                errors.append(f"{label}: missing {sorted(missing)}")
            record_id = record.get("document_id", "")
            if record.get("schema_version") != "0.1" or record.get("record_type") != "document":
                errors.append(f"{label}: schema_version/record_type mismatch")
            if not PATTERNS["document"].fullmatch(record_id):
                errors.append(f"{label}: malformed document_id")
            source = record.get("source", {})
            expected = stable_id("doc", {
                "relative_path": source.get("relative_path"), "source_sha256": source.get("sha256"),
            })
            if record_id != expected:
                errors.append(f"{label}: unstable document id")
            try:
                connection.execute("INSERT INTO documents VALUES (?, ?)", (record_id, source.get("relative_path", "")))
            except sqlite3.IntegrityError:
                errors.append(f"{label}: duplicate document id {record_id}")
            if source_root is not None:
                root = source_root.resolve()
                source_path = (root / source.get("relative_path", "")).resolve()
                try:
                    source_path.relative_to(root)
                except ValueError:
                    errors.append(f"{label}: source path escapes root")
                else:
                    if not source_path.is_file():
                        errors.append(f"{label}: source file is missing")
                    elif source_path.stat().st_size != source.get("size_bytes") or digest_file(source_path) != source.get("sha256"):
                        errors.append(f"{label}: source size or hash mismatch")
        connection.commit()

        for line_number, record in records(directory / "evidence.jsonl"):
            counts["evidence"] += 1
            label = f"evidence[{line_number}]"
            missing = REQUIRED["evidence"] - record.keys()
            if missing:
                errors.append(f"{label}: missing {sorted(missing)}")
            evidence_id = record.get("evidence_id", "")
            document_id = record.get("document_id", "")
            if record.get("schema_version") != "0.1" or record.get("record_type") != "evidence":
                errors.append(f"{label}: schema_version/record_type mismatch")
            if not PATTERNS["evidence"].fullmatch(evidence_id):
                errors.append(f"{label}: malformed evidence_id")
            item_content = record.get("content", {})
            try:
                if digest_value(content_hash_payload(item_content)) != item_content.get("sha256"):
                    errors.append(f"{label}: content hash mismatch")
                expected = stable_id("ev", {
                    "document_id": document_id,
                    "evidence_type": record.get("evidence_type"),
                    "location": record.get("location"),
                    "content_sha256": item_content.get("sha256"),
                })
                if evidence_id != expected:
                    errors.append(f"{label}: unstable evidence id")
                if "raw_text" in item_content and item_content.get("normalized_text") != normalize_text(item_content["raw_text"]):
                    errors.append(f"{label}: normalized_text mismatch")
                if "raw_value" in item_content and item_content.get("normalized_value") != item_content["raw_value"]:
                    errors.append(f"{label}: normalized_value mismatch")
            except ValueError as exc:
                errors.append(f"{label}: {exc}")
            try:
                connection.execute(
                    "INSERT INTO evidence VALUES (?, ?, ?)",
                    (evidence_id, document_id, record.get("parent_evidence_id")),
                )
            except sqlite3.IntegrityError:
                errors.append(f"{label}: duplicate evidence id {evidence_id}")
            if counts["evidence"] % 100000 == 0:
                connection.commit()
        connection.commit()

        for document_id, count in connection.execute(
            "SELECT e.document_id, COUNT(*) FROM evidence e LEFT JOIN documents d ON d.id=e.document_id "
            "WHERE d.id IS NULL GROUP BY e.document_id LIMIT 100"
        ):
            errors.append(f"{count} Evidence record(s) have dangling document_id {document_id}")
        for evidence_id, parent_id in connection.execute(
            "SELECT child.id, child.parent_id FROM evidence child LEFT JOIN evidence parent ON parent.id=child.parent_id "
            "WHERE child.parent_id IS NOT NULL AND parent.id IS NULL LIMIT 100"
        ):
            errors.append(f"{evidence_id}: dangling parent {parent_id}")
        for evidence_id, parent_id in connection.execute(
            "SELECT child.id, child.parent_id FROM evidence child JOIN evidence parent ON parent.id=child.parent_id "
            "WHERE child.document_id != parent.document_id LIMIT 100"
        ):
            errors.append(f"{evidence_id}: parent {parent_id} belongs to another document")

        for line_number, record in records(directory / "relations.jsonl"):
            counts["relation"] += 1
            label = f"relation[{line_number}]"
            missing = REQUIRED["relation"] - record.keys()
            if missing:
                errors.append(f"{label}: missing {sorted(missing)}")
            relation_id = record.get("relation_id", "")
            if record.get("schema_version") != "0.1" or record.get("record_type") != "relation":
                errors.append(f"{label}: schema_version/record_type mismatch")
            if not PATTERNS["relation"].fullmatch(relation_id):
                errors.append(f"{label}: malformed relation_id")
            expected = stable_id("rel", {
                "class": record.get("relation_class"),
                "type": record.get("relation_type"),
                "from": record.get("from_ref"),
                "to": record.get("to_ref"),
                "generator": record.get("provenance", {}).get("generated_by"),
                "generator_version": record.get("provenance", {}).get("generator_version"),
            })
            if relation_id != expected:
                errors.append(f"{label}: unstable relation id")
            try:
                connection.execute("INSERT INTO relations VALUES (?)", (relation_id,))
            except sqlite3.IntegrityError:
                errors.append(f"{label}: duplicate relation id {relation_id}")
            for side in ("from_ref", "to_ref"):
                ref = record.get(side, {})
                kind = ref.get("record_type", "")
                record_id = ref.get("record_id", "")
                if kind not in {"document", "evidence"}:
                    errors.append(f"{label}: invalid {side} type {kind!r}")
                else:
                    connection.execute("INSERT INTO refs VALUES (?, ?, ?, ?)", (relation_id, side, kind, record_id))
            connection.executemany(
                "INSERT INTO supporting VALUES (?, ?)",
                ((relation_id, evidence_id) for evidence_id in record.get("supporting_evidence_ids", [])),
            )
            if counts["relation"] % 100000 == 0:
                connection.commit()
        connection.commit()

        for relation_id, side, kind, record_id in connection.execute(
            "SELECT r.relation_id,r.side,r.kind,r.record_id FROM refs r "
            "LEFT JOIN documents d ON r.kind='document' AND d.id=r.record_id "
            "LEFT JOIN evidence e ON r.kind='evidence' AND e.id=r.record_id "
            "WHERE (r.kind='document' AND d.id IS NULL) OR (r.kind='evidence' AND e.id IS NULL) LIMIT 100"
        ):
            errors.append(f"{relation_id}: dangling {side} {kind} {record_id}")
        for relation_id, evidence_id in connection.execute(
            "SELECT s.relation_id,s.evidence_id FROM supporting s LEFT JOIN evidence e ON e.id=s.evidence_id "
            "WHERE e.id IS NULL LIMIT 100"
        ):
            errors.append(f"{relation_id}: dangling supporting Evidence {evidence_id}")
        connection.close()

    if errors:
        preview = errors[:100]
        suffix = f"\n- ... {len(errors) - 100} more" if len(errors) > 100 else ""
        raise ValueError("validation failed:\n- " + "\n- ".join(preview) + suffix)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    print(canonical_json({
        "status": "ok",
        "counts": validate(args.directory.resolve(), args.root.resolve() if args.root else None),
    }))


if __name__ == "__main__":
    main()
