#!/usr/bin/env python3
"""Validate local semantic index identity, shape, hashes, and vector norms."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from lexical_search_common import canonical_json, digest_file


def validate(index_directory: Path, search_output: Path) -> dict[str, object]:
    state_path = index_directory / "semantic-index-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    search_state_path = search_output / "search-build-state.json"
    search_state = json.loads(search_state_path.read_text(encoding="utf-8"))
    search_units_path = search_output / search_state.get("output", {}).get("relative_path", "")
    errors: list[str] = []
    matrix_path = index_directory / state.get("matrix", {}).get("relative_path", "")
    documents_path = index_directory / state.get("documents", {}).get("relative_path", "")
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
    record_count = 0
    dimensions = 0
    if matrix_path.is_file() and documents_path.is_file():
        matrix = np.load(matrix_path, mmap_mode="r", allow_pickle=False)
        documents = [json.loads(line) for line in documents_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        search_units = [json.loads(line) for line in search_units_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        record_count = len(documents)
        dimensions = int(matrix.shape[1]) if matrix.ndim == 2 else 0
        expected_shape = (state.get("matrix", {}).get("record_count"), state.get("matrix", {}).get("dimensions"))
        if matrix.shape != expected_shape or record_count != state.get("documents", {}).get("record_count"):
            errors.append("semantic matrix or document count mismatch")
        if matrix.dtype != np.float32 or not np.isfinite(matrix).all():
            errors.append("semantic matrix dtype or values are invalid")
        norms = np.linalg.norm(matrix, axis=1)
        if not np.allclose(norms, 1.0, rtol=1e-5, atol=1e-6):
            errors.append("semantic vectors are not L2-normalized")
        ids = [document.get("search_unit_id") for document in documents]
        if len(ids) != len(set(ids)):
            errors.append("semantic documents contain duplicate SearchUnit IDs")
        expected_documents = [
            {
                "search_unit_id": unit["search_unit_id"],
                "document_id": unit["document_id"],
                "unit_type": unit["unit_type"],
                "locator": unit["locator"],
                "source_evidence_ids": unit["source_evidence_ids"],
                "search_text": unit["text"]["search_text"],
            }
            for unit in search_units
        ]
        if documents != expected_documents:
            errors.append("semantic documents do not match source SearchUnits in order or content")
    if errors:
        raise ValueError("validation failed:\n- " + "\n- ".join(errors))
    return {"records": record_count, "dimensions": dimensions, "model": state["model"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index_directory", type=Path)
    parser.add_argument("--search-output", required=True, type=Path)
    args = parser.parse_args()
    print(canonical_json({"status": "ok", **validate(args.index_directory.resolve(), args.search_output.resolve())}))


if __name__ == "__main__":
    main()
