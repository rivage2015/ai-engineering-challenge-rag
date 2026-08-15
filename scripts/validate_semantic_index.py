#!/usr/bin/env python3
"""Validate semantic index identity, hashes, order, and vector norms."""

from __future__ import annotations

import argparse
import json
from itertools import zip_longest
from pathlib import Path

import numpy as np

from lexical_search_common import canonical_json, digest_file


def file_is_exact_prefix(base: Path, combined: Path) -> bool:
    with base.open("rb") as left, combined.open("rb") as right:
        while chunk := left.read(1024 * 1024):
            if right.read(len(chunk)) != chunk:
                return False
    return True


def validate(
    index_directory: Path,
    search_output: Path,
    base_index: Path | None = None,
) -> dict[str, object]:
    state_path = index_directory / "semantic-index-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    search_state_path = search_output / "search-build-state.json"
    search_state = json.loads(search_state_path.read_text(encoding="utf-8"))
    search_units_path = search_output / search_state.get("output", {}).get("relative_path", "")
    errors: list[str] = []
    matrix_path = index_directory / state.get("matrix", {}).get("relative_path", "")
    documents_path = index_directory / state.get("documents", {}).get("relative_path", "")
    offset_state = state.get("document_offsets")
    offsets_path = index_directory / offset_state.get("relative_path", "") if offset_state else None
    if state.get("build_status") != "complete" or state.get("normalization") != "l2":
        errors.append("semantic state is not complete or normalized")
    if state.get("source", {}).get("search_units_sha256") != search_state.get("output", {}).get("sha256"):
        errors.append("SearchUnit source hash mismatch")
    if state.get("source", {}).get("search_state_sha256") != digest_file(search_state_path):
        errors.append("search state source hash mismatch")
    for path, section in ((matrix_path, "matrix"), (documents_path, "documents")):
        if not path.is_file():
            errors.append(f"{section} file is missing")
        elif digest_file(path) != state.get(section, {}).get("sha256"):
            errors.append(f"{section} hash mismatch")
    if offsets_path is not None:
        if not offsets_path.is_file():
            errors.append("document_offsets file is missing")
        elif digest_file(offsets_path) != offset_state.get("sha256"):
            errors.append("document_offsets hash mismatch")
    base_source = state.get("source", {}).get("base_index")
    reused_base_records = 0
    if base_source is not None and base_index is None:
        errors.append("reused semantic index requires --base-index for exact prefix validation")
    if base_source is None and base_index is not None:
        errors.append("--base-index was supplied but semantic state records no reused base")
    record_count = 0
    dimensions = 0
    if matrix_path.is_file() and documents_path.is_file():
        matrix = np.load(matrix_path, mmap_mode="r", allow_pickle=False)
        record_count = int(state.get("documents", {}).get("record_count", 0))
        dimensions = int(matrix.shape[1]) if matrix.ndim == 2 else 0
        expected_shape = (state.get("matrix", {}).get("record_count"), state.get("matrix", {}).get("dimensions"))
        if matrix.shape != expected_shape or record_count != state.get("matrix", {}).get("record_count"):
            errors.append("semantic matrix or document count mismatch")
        if matrix.dtype != np.float32:
            errors.append("semantic matrix dtype is invalid")
        for start in range(0, len(matrix), 8192):
            batch = np.asarray(matrix[start:start + 8192])
            if not np.isfinite(batch).all():
                errors.append("semantic matrix contains non-finite values")
                break
            if not np.allclose(np.linalg.norm(batch, axis=1), 1.0, rtol=1e-5, atol=1e-6):
                errors.append("semantic vectors are not L2-normalized")
                break
        offsets = None
        if offsets_path and offsets_path.is_file():
            offsets = np.load(offsets_path, mmap_mode="r", allow_pickle=False)
            if offsets.dtype != np.int64 or offsets.shape != (record_count + 1,):
                errors.append("document offsets shape or dtype is invalid")
                offsets = None
            elif int(offsets[0]) != 0 or int(offsets[-1]) != documents_path.stat().st_size:
                errors.append("document offsets do not cover the JSONL file")
        if base_source is not None and base_index is not None:
            base_state_path = base_index / "semantic-index-state.json"
            base_state = json.loads(base_state_path.read_text(encoding="utf-8"))
            base_matrix_path = base_index / base_state.get("matrix", {}).get("relative_path", "")
            base_documents_path = base_index / base_state.get("documents", {}).get("relative_path", "")
            base_offset_state = base_state.get("document_offsets", {})
            base_offsets_path = base_index / base_offset_state.get("relative_path", "")
            reused_base_records = int(base_source.get("record_count", 0))
            provenance_matches = (
                digest_file(base_state_path) == base_source.get("semantic_state_sha256")
                and base_state.get("matrix", {}).get("sha256") == base_source.get("matrix_sha256")
                and base_state.get("documents", {}).get("sha256") == base_source.get("documents_sha256")
                and base_state.get("matrix", {}).get("record_count") == reused_base_records
            )
            if not provenance_matches:
                errors.append("reused base semantic state does not match recorded provenance")
            elif not base_matrix_path.is_file() or not base_documents_path.is_file():
                errors.append("reused base semantic files are missing")
            elif (
                digest_file(base_matrix_path) != base_source.get("matrix_sha256")
                or digest_file(base_documents_path) != base_source.get("documents_sha256")
            ):
                errors.append("reused base semantic file hash mismatch")
            else:
                base_matrix = np.load(base_matrix_path, mmap_mode="r", allow_pickle=False)
                if base_matrix.shape != (reused_base_records, dimensions):
                    errors.append("reused base semantic matrix shape mismatch")
                else:
                    for start in range(0, reused_base_records, 8192):
                        end = min(start + 8192, reused_base_records)
                        if not np.array_equal(base_matrix[start:end], matrix[start:end]):
                            errors.append(f"semantic vector prefix differs at rows {start}-{end - 1}")
                            break
                if not file_is_exact_prefix(base_documents_path, documents_path):
                    errors.append("semantic document JSONL is not an exact base prefix")
                if offsets is None or not base_offsets_path.is_file():
                    errors.append("semantic offset prefix cannot be validated")
                else:
                    base_offsets = np.load(base_offsets_path, mmap_mode="r", allow_pickle=False)
                    if not np.array_equal(base_offsets, offsets[:len(base_offsets)]):
                        errors.append("semantic document offsets are not an exact base prefix")
        ids: set[str] = set()
        document_rows = 0
        with documents_path.open("rb") as documents, search_units_path.open(encoding="utf-8") as search_units:
            document_lines = (line for line in documents if line.strip())
            search_lines = (line for line in search_units if line.strip())
            for row_index, (document_line, search_line) in enumerate(zip_longest(document_lines, search_lines)):
                if document_line is None or search_line is None:
                    errors.append("semantic documents and SearchUnits have different row counts")
                    break
                unit = json.loads(search_line)
                document = json.loads(document_line.decode("utf-8"))
                expected = {
                    "search_unit_id": unit["search_unit_id"],
                    "document_id": unit["document_id"],
                    "unit_type": unit["unit_type"],
                    "locator": unit["locator"],
                    "source_evidence_ids": unit["source_evidence_ids"],
                    "search_text": unit["text"]["search_text"],
                }
                if document != expected:
                    errors.append(f"semantic document row {row_index} does not match its SearchUnit")
                    break
                search_unit_id = document.get("search_unit_id")
                if search_unit_id in ids:
                    errors.append(f"duplicate semantic SearchUnit ID: {search_unit_id}")
                    break
                ids.add(search_unit_id)
                document_rows += 1
        if document_rows != record_count:
            errors.append(f"semantic document count mismatch: expected {record_count}, found {document_rows}")
        if offsets is not None:
            previous = int(offsets[0])
            for index in range(1, len(offsets)):
                current = int(offsets[index])
                if current <= previous:
                    errors.append(f"document offsets are not strictly increasing at row {index}")
                    break
                previous = current
    if errors:
        raise ValueError("validation failed:\n- " + "\n- ".join(dict.fromkeys(errors)))
    return {
        "records": record_count,
        "dimensions": dimensions,
        "model": state["model"],
        "reused_base_records": reused_base_records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index_directory", type=Path)
    parser.add_argument("--search-output", required=True, type=Path)
    parser.add_argument("--base-index", type=Path)
    args = parser.parse_args()
    print(canonical_json({
        "status": "ok",
        **validate(
            args.index_directory.resolve(),
            args.search_output.resolve(),
            args.base_index.resolve() if args.base_index else None,
        ),
    }))


if __name__ == "__main__":
    main()
