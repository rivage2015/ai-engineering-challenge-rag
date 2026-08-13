#!/usr/bin/env python3
"""Build a traceable local semantic index from SearchUnits through Ollama."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import numpy as np

from lexical_search_common import canonical_json, digest_file
from ollama_embedding_common import DEFAULT_BASE_URL, DEFAULT_MODEL, embed_texts, model_info


BUILDER = "ollama-semantic-index-builder"
BUILDER_VERSION = "0.1.0"
STATE_FILE = "semantic-index-state.json"
MATRIX_FILE = "semantic-index.npy"
DOCUMENTS_FILE = "semantic-documents.jsonl"
DOCUMENT_PROMPT = "title: none | text: {text}"
QUERY_PROMPT = "task: search result | query: {query}"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def prepare_output(output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)


def build(
    search_output: Path,
    output: Path,
    base_url: str,
    model: str,
    batch_size: int,
    timeout: float,
) -> dict[str, Any]:
    if batch_size < 1 or batch_size > 256:
        raise ValueError("batch_size must be between 1 and 256")
    state_path = search_output / "search-build-state.json"
    search_state = load_json(state_path)
    if search_state.get("build_status") != "complete":
        raise ValueError("search unit build must be complete")
    units_path = search_output / search_state.get("output", {}).get("relative_path", "")
    expected_sha = search_state.get("output", {}).get("sha256")
    if not units_path.is_file() or digest_file(units_path) != expected_sha:
        raise ValueError("SearchUnit file does not match search-build-state.json")
    units = [json.loads(line) for line in units_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not units:
        raise ValueError("SearchUnit file is empty")
    prepare_output(output)
    installed_model = model_info(base_url, model, timeout=min(timeout, 30.0))
    matrices: list[np.ndarray] = []
    dimensions: int | None = None
    for start in range(0, len(units), batch_size):
        texts = [
            DOCUMENT_PROMPT.format(text=unit["text"]["search_text"])
            for unit in units[start:start + batch_size]
        ]
        batch = np.asarray(embed_texts(base_url, model, texts, timeout), dtype=np.float32)
        if batch.ndim != 2 or not np.isfinite(batch).all():
            raise RuntimeError("embedding matrix is not finite and two-dimensional")
        dimensions = dimensions or int(batch.shape[1])
        if batch.shape[1] != dimensions:
            raise RuntimeError("embedding dimensions changed during the build")
        norms = np.linalg.norm(batch, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise RuntimeError("Ollama returned a zero-length embedding")
        matrices.append(batch / norms)
    matrix = np.concatenate(matrices, axis=0)
    temporary_matrix = output / f".{MATRIX_FILE}.tmp"
    with temporary_matrix.open("wb") as handle:
        np.save(handle, matrix, allow_pickle=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_matrix, output / MATRIX_FILE)
    temporary_documents = output / f".{DOCUMENTS_FILE}.tmp"
    with temporary_documents.open("w", encoding="utf-8", newline="\n") as handle:
        for unit in units:
            handle.write(canonical_json({
                "search_unit_id": unit["search_unit_id"],
                "document_id": unit["document_id"],
                "unit_type": unit["unit_type"],
                "locator": unit["locator"],
                "source_evidence_ids": unit["source_evidence_ids"],
                "search_text": unit["text"]["search_text"],
            }) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_documents, output / DOCUMENTS_FILE)
    result = {
        "state_version": "1",
        "build_status": "complete",
        "builder": BUILDER,
        "builder_version": BUILDER_VERSION,
        "normalization": "l2",
        "prompts": {"document": DOCUMENT_PROMPT, "query": QUERY_PROMPT},
        "model": installed_model,
        "source": {
            "search_units_sha256": expected_sha,
            "search_state_sha256": digest_file(state_path),
        },
        "matrix": {
            "relative_path": MATRIX_FILE,
            "sha256": digest_file(output / MATRIX_FILE),
            "size_bytes": (output / MATRIX_FILE).stat().st_size,
            "record_count": int(matrix.shape[0]),
            "dimensions": int(matrix.shape[1]),
            "dtype": "float32",
        },
        "documents": {
            "relative_path": DOCUMENTS_FILE,
            "sha256": digest_file(output / DOCUMENTS_FILE),
            "size_bytes": (output / DOCUMENTS_FILE).stat().st_size,
            "record_count": len(units),
        },
    }
    temporary_state = output / f".{STATE_FILE}.tmp"
    with temporary_state.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(result) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_state, output / STATE_FILE)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search-output", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()
    print(canonical_json(build(
        args.search_output.resolve(), args.out.resolve(), args.base_url, args.model,
        args.batch_size, args.timeout,
    )))


if __name__ == "__main__":
    main()
