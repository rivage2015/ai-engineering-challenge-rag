#!/usr/bin/env python3
"""Validate Layer-1 deliverable hashes, pairing, inventory, and chunk links."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

from build_layer1_deliverables import (
    EVAL_FIELDS,
    EXPERIMENT_FIELDS,
    INVENTORY_FIELDS,
    ISSUE_FIELDS,
    normalize_page_with_edges,
)
from lexical_search_common import canonical_json, digest_file
from probe_intermediate_records import normalize_text, stable_id


REQUIRED_FILES = {
    "text_inventory.csv", "native_text_raw.jsonl", "native_text_normalized.jsonl",
    "text_extraction_issues.csv", "text_chunks.jsonl", "text_retrieval_eval.csv",
    "text_retrieval_summary.md", "text_error_analysis.md", "text_experiment_log.csv",
}
VALID_LAYERS = {"native_text", "ocr_required", "graph_required", "unsupported"}
GROUND_TRUTH_STATUSES = {"confirmed", "provisional", "needs_human_review"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc


def validate(directory: Path) -> dict[str, Any]:
    state_path = directory / "layer1-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if state.get("build_status") != "complete":
        errors.append("layer1-state.json is not complete")
    inputs = state.get("inputs", {})
    intermediate_input = inputs.get("intermediate", {})
    search_input = inputs.get("search_output", {})
    evaluation_inputs = inputs.get("evaluation_reports", [])
    if not inputs.get("source_root"):
        errors.append("layer1-state.json has no source root")
    for label, record in (("intermediate", intermediate_input), ("search_output", search_input)):
        if not record.get("path") or not SHA256.fullmatch(str(record.get("state_sha256", ""))):
            errors.append(f"layer1-state.json has invalid {label} provenance")
    if not SHA256.fullmatch(str(search_input.get("search_units_sha256", ""))):
        errors.append("layer1-state.json has invalid SearchUnit hash")
    if not isinstance(search_input.get("target_chars"), int) or search_input.get("target_chars", 0) < 100:
        errors.append("layer1-state.json has invalid chunk target")
    if not evaluation_inputs or any(
        not item.get("path") or not SHA256.fullmatch(str(item.get("sha256", "")))
        for item in evaluation_inputs
    ):
        errors.append("layer1-state.json has invalid evaluation provenance")
    state_files = state.get("files", {})
    if set(state_files) != REQUIRED_FILES:
        errors.append("layer1-state.json file inventory does not match required outputs")
    for name in REQUIRED_FILES:
        path = directory / name
        expected = state_files.get(name, {})
        if not path.is_file():
            errors.append(f"missing output: {name}")
            continue
        if path.stat().st_size != expected.get("size_bytes"):
            errors.append(f"size mismatch: {name}")
        if digest_file(path) != expected.get("sha256"):
            errors.append(f"hash mismatch: {name}")

    inventory_count = 0
    inventory_ids: set[str] = set()
    inventory_paths: set[str] = set()
    with (directory / "text_inventory.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != INVENTORY_FIELDS:
            errors.append("text_inventory.csv header mismatch")
        for line_number, row in enumerate(reader, 2):
            inventory_count += 1
            file_id = row.get("file_id", "")
            path = row.get("file_path", "")
            source_sha = row.get("source_sha256", "")
            if not file_id or file_id in inventory_ids:
                errors.append(f"text_inventory.csv:{line_number}: missing or duplicate file_id")
            if not path or path in inventory_paths:
                errors.append(f"text_inventory.csv:{line_number}: missing or duplicate file_path")
            inventory_ids.add(file_id)
            inventory_paths.add(path)
            if not SHA256.fullmatch(source_sha):
                errors.append(f"text_inventory.csv:{line_number}: invalid source_sha256")
            elif file_id != stable_id("file", {"relative_path": path, "source_sha256": source_sha}):
                errors.append(f"text_inventory.csv:{line_number}: unstable file_id")
            layers = {item for item in row.get("processing_layer", "").split(";") if item}
            if not layers or not layers <= VALID_LAYERS:
                errors.append(f"text_inventory.csv:{line_number}: invalid processing layer")

    issue_count = 0
    issue_ids: set[str] = set()
    with (directory / "text_extraction_issues.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ISSUE_FIELDS:
            errors.append("text_extraction_issues.csv header mismatch")
        for line_number, row in enumerate(reader, 2):
            issue_count += 1
            issue_id = row.get("issue_id", "")
            if not issue_id or issue_id in issue_ids:
                errors.append(f"text_extraction_issues.csv:{line_number}: missing or duplicate issue_id")
            if row.get("file_id") not in inventory_ids:
                errors.append(f"text_extraction_issues.csv:{line_number}: file_id absent from inventory")
            if row.get("severity") not in {"info", "warning", "error"}:
                errors.append(f"text_extraction_issues.csv:{line_number}: invalid severity")
            if row.get("status") not in {"unresolved", "deferred", "resolved"}:
                errors.append(f"text_extraction_issues.csv:{line_number}: invalid status")
            issue_ids.add(issue_id)

    raw_count = 0
    raw_page_edges: Counter[tuple[str, str, str]] = Counter()
    normalized_edge_operations: Counter[tuple[str, str, str]] = Counter()
    raw_iter = jsonl(directory / "native_text_raw.jsonl")
    normalized_iter = jsonl(directory / "native_text_normalized.jsonl")
    while True:
        try:
            raw = next(raw_iter)
        except StopIteration:
            raw = None
        try:
            normalized = next(normalized_iter)
        except StopIteration:
            normalized = None
        if raw is None or normalized is None:
            if raw is not None or normalized is not None:
                errors.append("raw and normalized JSONL record counts differ")
            break
        raw_count += 1
        identity = ("file_id", "document_id", "evidence_id", "source_path", "evidence_type", "location")
        if any(raw.get(key) != normalized.get(key) for key in identity):
            errors.append(f"raw/normalized identity mismatch at record {raw_count}")
            if len(errors) > 100:
                break
        raw_fields = {key for key in ("raw_text", "raw_value") if key in raw}
        normalized_fields = {key for key in ("normalized_text", "normalized_value") if key in normalized}
        if len(raw_fields) != 1 or len(normalized_fields) != 1:
            errors.append(f"raw/normalized value missing or ambiguous at record {raw_count}")
        elif ("raw_text" in raw) != ("normalized_text" in normalized):
            errors.append(f"raw/normalized value kinds differ at record {raw_count}")
        elif "raw_text" in raw:
            operations = normalized.get("normalization_operations", [])
            if operations:
                operations_valid = raw.get("evidence_type") == "page" and not any(
                    set(operation) != {"operation", "text"}
                    or operation.get("operation") not in {
                        "remove_repeated_pdf_header", "remove_repeated_pdf_footer",
                    }
                    for operation in operations
                )
                if not operations_valid:
                    errors.append(f"invalid normalization operation at record {raw_count}")
                else:
                    edge_sets = {
                        "first": {
                            operation["text"] for operation in operations
                            if operation.get("operation") == "remove_repeated_pdf_header"
                        },
                        "last": {
                            operation["text"] for operation in operations
                            if operation.get("operation") == "remove_repeated_pdf_footer"
                        },
                    }
                    expected_text, expected_operations = normalize_page_with_edges(raw["raw_text"], edge_sets)
                    if normalized["normalized_text"] != expected_text or operations != expected_operations:
                        errors.append(f"normalized PDF edge removal mismatch at record {raw_count}")
                    for operation in operations:
                        normalized_edge_operations[(
                            raw.get("document_id", ""), operation["operation"], operation["text"],
                        )] += 1
            elif normalized["normalized_text"] != normalize_text(raw["raw_text"]):
                errors.append(f"normalized text mismatch at record {raw_count}")
            if raw.get("evidence_type") == "page" and raw["raw_text"].strip():
                lines = [line.strip() for line in normalize_text(raw["raw_text"]).splitlines() if line.strip()]
                if lines:
                    raw_page_edges[(raw.get("document_id", ""), "remove_repeated_pdf_header", lines[0])] += 1
                    raw_page_edges[(raw.get("document_id", ""), "remove_repeated_pdf_footer", lines[-1])] += 1
        elif "raw_value" in raw and normalized["normalized_value"] != raw["raw_value"]:
            errors.append(f"normalized value mismatch at record {raw_count}")
        if raw.get("file_id") not in inventory_ids or raw.get("source_path") not in inventory_paths:
            errors.append(f"native text record {raw_count}: source absent from inventory")
    for key in normalized_edge_operations:
        if raw_page_edges[key] < 3:
            errors.append(f"PDF edge normalization is not supported by three repeated pages: {key}")

    chunk_count = 0
    chunk_ids: set[str] = set()
    previous: dict[str, Any] | None = None
    for chunk in jsonl(directory / "text_chunks.jsonl"):
        chunk_count += 1
        chunk_id = chunk.get("chunk_id", "")
        if not chunk_id or chunk_id in chunk_ids:
            errors.append(f"chunk {chunk_id}: missing or duplicate chunk_id")
        chunk_ids.add(chunk_id)
        if chunk.get("source_path") not in inventory_paths:
            errors.append(f"chunk {chunk.get('chunk_id')}: source_path absent from inventory")
        if not chunk.get("chunk_text") or not chunk.get("source_evidence_ids"):
            errors.append(f"chunk {chunk.get('chunk_id')}: text or Evidence trace is missing")
        if chunk.get("modality") not in {"native_text", "chart_table"}:
            errors.append(f"chunk {chunk.get('chunk_id')}: invalid modality")
        if previous is not None:
            same_document = previous["document_id"] == chunk["document_id"]
            expected_previous = previous["chunk_id"] if same_document else None
            expected_next = chunk["chunk_id"] if same_document else None
            if chunk.get("previous_chunk_id") != expected_previous:
                errors.append(f"chunk {chunk.get('chunk_id')}: previous link mismatch")
            if previous.get("next_chunk_id") != expected_next:
                errors.append(f"chunk {previous.get('chunk_id')}: next link mismatch")
        previous = chunk
    if previous is not None and previous.get("next_chunk_id") is not None:
        errors.append("last chunk has a next link")

    evaluation_methods: set[str] = set()
    evaluation_rows = 0
    with (directory / "text_retrieval_eval.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != EVAL_FIELDS:
            errors.append("text_retrieval_eval.csv header mismatch")
        for line_number, row in enumerate(reader, 2):
            evaluation_rows += 1
            method = row.get("retrieval_method", "")
            if not method:
                errors.append(f"text_retrieval_eval.csv:{line_number}: missing retrieval method")
            evaluation_methods.add(method)
            if row.get("ground_truth_status") not in GROUND_TRUTH_STATUSES:
                errors.append(f"text_retrieval_eval.csv:{line_number}: invalid Ground Truth status")
            for cutoff in (1, 3, 5, 10):
                if row.get(f"hit_at_{cutoff}") not in {"0", "1"}:
                    errors.append(f"text_retrieval_eval.csv:{line_number}: invalid Hit@{cutoff}")

    experiment_methods: set[str] = set()
    experiment_report_hashes: set[str] = set()
    with (directory / "text_experiment_log.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != EXPERIMENT_FIELDS:
            errors.append("text_experiment_log.csv header mismatch")
        for line_number, row in enumerate(reader, 2):
            method = row.get("retrieval_method", "")
            experiment_methods.add(method)
            experiment_report_hashes.add(row.get("report_sha256", ""))
            if not row.get("experiment_id") or len(row.get("report_sha256", "")) != 64:
                errors.append(f"text_experiment_log.csv:{line_number}: invalid experiment identity")
    if not evaluation_rows or not evaluation_methods or evaluation_methods != experiment_methods:
        errors.append("retrieval evaluation and experiment methods are empty or inconsistent")

    counts = state.get("counts", {})
    if inventory_count != counts.get("inventory_files"):
        errors.append("inventory count does not match layer1-state.json")
    if issue_count != counts.get("issues"):
        errors.append("issue count does not match layer1-state.json")
    if raw_count != counts.get("native_text_records"):
        errors.append("native text count does not match layer1-state.json")
    if sum(normalized_edge_operations.values()) != counts.get("normalization_operations"):
        errors.append("normalization operation count does not match layer1-state.json")
    if chunk_count != sum(counts.get("chunks_by_type", {}).values()):
        errors.append("chunk count does not match layer1-state.json")
    if chunk_count != search_input.get("record_count"):
        errors.append("chunk count does not match input SearchUnit count")
    if len(evaluation_inputs) != len(experiment_methods):
        errors.append("evaluation provenance count does not match experiment methods")
    if {item.get("sha256") for item in evaluation_inputs} != experiment_report_hashes:
        errors.append("evaluation provenance hashes do not match experiment log")
    if raw_count != sum(counts.get("evidence_by_type", {}).values()):
        # Evidence with content_ref only is intentionally absent from the two native-text views.
        if raw_count > sum(counts.get("evidence_by_type", {}).values()):
            errors.append("native text count exceeds total Evidence count")
    if errors:
        preview = errors[:100]
        suffix = f"\n- ... {len(errors) - 100} more" if len(errors) > 100 else ""
        raise ValueError("validation failed:\n- " + "\n- ".join(preview) + suffix)
    return {
        "status": "ok",
        "inventory_files": inventory_count,
        "native_text_records": raw_count,
        "chunks": chunk_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(canonical_json(validate(args.directory.resolve())))


if __name__ == "__main__":
    main()
