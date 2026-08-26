#!/usr/bin/env python3
"""Adapt verified Layer 1 intermediate records to Local Memory Evidence.

This is a one-way, question-independent boundary adapter.  It does not answer
questions, execute document instructions, or bypass the Local Memory content
security gate.  Its outputs must be classified before any answer index is
built.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from validate_search_units import validate as validate_search_units


ADAPTER = "layer1-to-local-memory-evidence-adapter"
ADAPTER_VERSION = "0.4.0"
SCHEMA_VERSION = "0.1"


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return records


def atomic_write(path: Path, value: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def text_from_content(content: dict[str, Any]) -> tuple[str, str]:
    if isinstance(content.get("raw_text"), str):
        return content["raw_text"], "raw_text"
    if "raw_value" in content:
        return canonical(content["raw_value"]), "canonical_raw_value"
    raise ValueError("Evidence content has neither raw_text nor raw_value")


def validate_source_binding(root: Path, source: dict[str, Any]) -> None:
    relative = source.get("relative_path")
    if not isinstance(relative, str) or not relative:
        raise ValueError("Document source has no relative_path")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"source escapes root: {relative}") from exc
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"source is not a regular file: {relative}")
    expected_hash = source.get("sha256")
    if not isinstance(expected_hash, str) or sha256_file(path) != expected_hash:
        raise ValueError(f"source hash mismatch: {relative}")
    if path.stat().st_size != source.get("size_bytes"):
        raise ValueError(f"source size mismatch: {relative}")


def adapt(
    intermediate: Path,
    source_root: Path,
    output: Path,
    search_output: Path | None = None,
) -> dict[str, Any]:
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    state_path = intermediate / "build-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("build_status") != "complete":
        raise ValueError("Layer 1 intermediate build must be complete without failures")
    if Path(state.get("source_root", "")).resolve() != source_root:
        raise ValueError("source root does not match Layer 1 build state")

    layer_documents = read_jsonl(intermediate / "documents.jsonl")
    layer_evidence = read_jsonl(intermediate / "evidence.jsonl")
    search_units: list[dict[str, Any]] = []
    if search_output is not None:
        validate_search_units(search_output, intermediate)
        search_units = read_jsonl(search_output / "search_units.jsonl")
    document_by_id: dict[str, dict[str, Any]] = {}
    for document in layer_documents:
        document_id = document.get("document_id")
        if not isinstance(document_id, str) or not document_id or document_id in document_by_id:
            raise ValueError(f"invalid or duplicate document_id: {document_id!r}")
        validate_source_binding(source_root, document["source"])
        document_by_id[document_id] = document
    state_document_ids = {
        entry.get("document_id") for entry in state.get("entries", {}).values()
    }
    if state_document_ids != set(document_by_id):
        raise ValueError("Document IDs do not match Layer 1 build state")

    evidence_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_evidence: set[str] = set()
    projections: list[dict[str, Any]] = []
    projection_methods: Counter[str] = Counter()
    skipped_binary_evidence = 0
    for record in layer_evidence:
        evidence_id = record.get("evidence_id")
        document_id = record.get("document_id")
        if not isinstance(evidence_id, str) or not evidence_id or evidence_id in seen_evidence:
            raise ValueError(f"invalid or duplicate evidence_id: {evidence_id!r}")
        if document_id not in document_by_id:
            raise ValueError(f"Evidence references missing document: {document_id}")
        seen_evidence.add(evidence_id)
        record_content = record.get("content", {})
        if isinstance(record_content.get("content_ref"), str):
            # Preserve the binary source binding in Layer 1, but do not turn a
            # path or opaque image payload into searchable text. Searchable
            # image content must arrive through separately audited OCR lines.
            skipped_binary_evidence += 1
            continue
        observed_text, projection_method = text_from_content(record_content)
        projection_methods[projection_method] += 1
        source = document_by_id[document_id]["source"]
        projected = {
            "schema_version": SCHEMA_VERSION,
            "evidence_id": evidence_id,
            "document_id": document_id,
            "ordinal": record.get("ordinal"),
            "locator": record.get("location", {}),
            "observed_text": observed_text,
            "source": {
                "relative_path": source["relative_path"],
                "sha256": source["sha256"],
            },
            "extraction_method": record.get("provenance", {}).get("extraction_method", "unknown"),
            "status": "observed",
            "adapter": {
                "name": ADAPTER,
                "version": ADAPTER_VERSION,
                "source_record_type": record.get("evidence_type"),
                "text_projection": projection_method,
                "execution_policy": "never_execute",
            },
        }
        geometry = record.get("geometry")
        if isinstance(geometry, dict) and geometry:
            projected["geometry"] = geometry
        evidence_by_document[document_id].append(projected)
        projections.append(projected)

    # SearchUnits are derived, question-independent groupings of verified
    # Evidence. Preserve table rows and audited image text packets at this
    # boundary because isolated cells or OCR lines lose their relationships.
    # Every referenced Evidence ID has already been validated against the same
    # intermediate build by validate_search_units().
    search_unit_projection_count = 0
    for unit in search_units:
        if unit.get("unit_type") not in {"table_row", "image_text_packet"}:
            continue
        document_id = unit["document_id"]
        source_evidence_ids = unit["source_evidence_ids"]
        observed_text = unit["text"]["search_text"]
        evidence_id = stable_id("ev", {
            "adapter": ADAPTER,
            "adapter_version": ADAPTER_VERSION,
            "source_search_unit_id": unit["search_unit_id"],
            "document_id": document_id,
            "unit_type": unit["unit_type"],
            "source_evidence_ids": source_evidence_ids,
            "locator": unit["locator"],
            "text_sha256": unit["text"]["sha256"],
        })
        if evidence_id in seen_evidence:
            raise ValueError(f"projected SearchUnit collides with Evidence ID: {evidence_id}")
        seen_evidence.add(evidence_id)
        source = document_by_id[document_id]["source"]
        projected = {
            "schema_version": SCHEMA_VERSION,
            "evidence_id": evidence_id,
            "document_id": document_id,
            "ordinal": len(evidence_by_document[document_id]) + 1,
            "locator": unit["locator"],
            "observed_text": observed_text,
            "source": {
                "relative_path": source["relative_path"],
                "sha256": source["sha256"],
            },
            "extraction_method": "verified_search_unit_projection",
            "status": "observed",
            "adapter": {
                "name": ADAPTER,
                "version": ADAPTER_VERSION,
                "source_record_type": "search_unit",
                "source_search_unit_id": unit["search_unit_id"],
                "source_evidence_ids": source_evidence_ids,
                "unit_type": unit["unit_type"],
                "text_projection": "search_unit_text",
                "execution_policy": "never_execute",
            },
        }
        evidence_by_document[document_id].append(projected)
        projections.append(projected)
        projection_methods["search_unit_text"] += 1
        search_unit_projection_count += 1

    documents: list[dict[str, Any]] = []
    statuses: Counter[str] = Counter()
    for source_document in layer_documents:
        document_id = source_document["document_id"]
        source = source_document["source"]
        status = source_document.get("extraction", {}).get("status", "unknown")
        statuses[status] += 1
        documents.append({
            "schema_version": SCHEMA_VERSION,
            "document_id": document_id,
            "source": {
                "relative_path": source["relative_path"],
                "absolute_path": str(source_root / source["relative_path"]),
                "sha256": source["sha256"],
                "size_bytes": source["size_bytes"],
                "file_type": source.get("extension") or "no_extension",
            },
            "classification": "extractable",
            "classification_reason": "verified_layer1_intermediate_record",
            "project_id": None,
            "extraction_method": source_document.get("extraction", {}).get("parser", "unknown"),
            "status": "extracted" if evidence_by_document.get(document_id) else "empty_after_extraction",
            "evidence_ids": [item["evidence_id"] for item in evidence_by_document.get(document_id, [])],
            "extraction_metadata": {
                "layer1_status": status,
                "layer1_parser_version": source_document.get("extraction", {}).get("parser_version"),
                "adapter": ADAPTER,
                "adapter_version": ADAPTER_VERSION,
            },
            "error": None,
        })

    document_bytes = "".join(canonical(item) + "\n" for item in documents).encode("utf-8")
    evidence_bytes = "".join(canonical(item) + "\n" for item in projections).encode("utf-8")
    documents_path = output / "semantic-documents.jsonl"
    evidence_path = output / "semantic-evidence.jsonl"
    atomic_write(documents_path, document_bytes)
    atomic_write(evidence_path, evidence_bytes)
    result = {
        "schema_version": SCHEMA_VERSION,
        "adapter": ADAPTER,
        "adapter_version": ADAPTER_VERSION,
        "question_independent": True,
        "execution_policy": "never_execute",
        "requires_content_security_gate": True,
        "source_root": str(source_root),
        "source_state": {"path": str(state_path), "sha256": sha256_file(state_path)},
        "inputs": {
            "documents_sha256": sha256_file(intermediate / "documents.jsonl"),
            "evidence_sha256": sha256_file(intermediate / "evidence.jsonl"),
        },
        "outputs": {
            "documents": {"path": documents_path.name, "sha256": sha256_file(documents_path), "count": len(documents)},
            "evidence": {"path": evidence_path.name, "sha256": sha256_file(evidence_path), "count": len(projections)},
        },
        "layer1_status_counts": dict(sorted(statuses.items())),
        "text_projection_counts": dict(sorted(projection_methods.items())),
        "search_unit_projection": {
            "enabled": search_output is not None,
            "included_unit_types": ["image_text_packet", "table_row"] if search_output is not None else [],
            "count": search_unit_projection_count,
            "search_state": ({
                "path": str(search_output / "search-build-state.json"),
                "sha256": sha256_file(search_output / "search-build-state.json"),
            } if search_output is not None else None),
            "search_units_sha256": (
                sha256_file(search_output / "search_units.jsonl") if search_output is not None else None
            ),
        },
        "skipped_binary_evidence": skipped_binary_evidence,
    }
    atomic_write(
        output / "layer1-adapter-state.json",
        (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intermediate", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--search-output", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = adapt(
        args.intermediate.resolve(strict=True),
        args.source_root.resolve(strict=True),
        args.out.resolve(),
        args.search_output.resolve(strict=True) if args.search_output is not None else None,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
