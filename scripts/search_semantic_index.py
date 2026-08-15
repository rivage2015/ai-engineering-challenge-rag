#!/usr/bin/env python3
"""Search a local normalized embedding matrix with cosine similarity."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from lexical_search_common import canonical_json, digest_file
from ollama_embedding_common import DEFAULT_BASE_URL, embed_texts, model_info
from retrieval_trace_common import enrich_retrieval, load_document_sources


@dataclass
class DocumentStore:
    path: Path
    offsets: np.ndarray | None
    legacy_documents: list[dict[str, Any]] | None

    def get_many(self, row_indices: list[int]) -> dict[int, dict[str, Any]]:
        if self.legacy_documents is not None:
            return {row_index: self.legacy_documents[row_index] for row_index in row_indices}
        if self.offsets is None:
            raise ValueError("semantic document store has neither offsets nor legacy records")
        records: dict[int, dict[str, Any]] = {}
        with self.path.open("rb") as handle:
            for row_index in row_indices:
                start = int(self.offsets[row_index])
                end = int(self.offsets[row_index + 1])
                handle.seek(start)
                records[row_index] = json.loads(handle.read(end - start).decode("utf-8"))
        return records


_INDEX_CACHE: dict[str, tuple[dict[str, Any], np.ndarray, DocumentStore]] = {}


def cosine_scores(matrix: np.ndarray, query_vector: np.ndarray, batch_rows: int = 8192) -> np.ndarray:
    """Compute finite cosine scores in bounded blocks with float64 accumulation."""
    if matrix.ndim != 2 or query_vector.shape != (matrix.shape[1],) or batch_rows < 1:
        raise ValueError("semantic matrix and query vector shapes are incompatible")
    if not np.isfinite(query_vector).all():
        raise ValueError("query embedding contains non-finite values")
    norm = float(np.linalg.norm(query_vector.astype(np.float64, copy=False)))
    if not np.isfinite(norm) or norm == 0:
        raise ValueError("query embedding has a non-finite or zero length")
    normalized = query_vector / norm
    scores = np.empty(matrix.shape[0], dtype=np.float64)
    for start in range(0, matrix.shape[0], batch_rows):
        end = min(start + batch_rows, matrix.shape[0])
        scores[start:end] = np.sum(
            np.asarray(matrix[start:end]) * normalized,
            axis=1,
            dtype=np.float64,
        )
    if not np.isfinite(scores).all():
        raise ValueError("semantic similarity scores contain non-finite values")
    return scores


def load_index(index_directory: Path) -> tuple[dict[str, Any], np.ndarray, DocumentStore]:
    cache_key = str(index_directory.resolve())
    cached = _INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached
    state_path = index_directory / "semantic-index-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("build_status") != "complete":
        raise ValueError("semantic index is incomplete")
    matrix_path = index_directory / state["matrix"]["relative_path"]
    documents_path = index_directory / state["documents"]["relative_path"]
    if digest_file(matrix_path) != state["matrix"]["sha256"]:
        raise ValueError("semantic matrix hash mismatch")
    if digest_file(documents_path) != state["documents"]["sha256"]:
        raise ValueError("semantic documents hash mismatch")
    matrix = np.load(matrix_path, mmap_mode="r", allow_pickle=False)
    expected_shape = (state["matrix"]["record_count"], state["matrix"]["dimensions"])
    if matrix.shape != expected_shape or matrix.dtype != np.float32:
        raise ValueError("semantic index shape or dtype mismatch")
    offset_state = state.get("document_offsets")
    if offset_state:
        offsets_path = index_directory / offset_state["relative_path"]
        if digest_file(offsets_path) != offset_state["sha256"]:
            raise ValueError("semantic document-offset hash mismatch")
        offsets = np.load(offsets_path, mmap_mode="r", allow_pickle=False)
        if offsets.shape != (state["documents"]["record_count"] + 1,) or offsets.dtype != np.int64:
            raise ValueError("semantic document offsets have an invalid shape or dtype")
        store = DocumentStore(documents_path, offsets, None)
    else:
        documents = [
            json.loads(line)
            for line in documents_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if len(documents) != state["documents"]["record_count"]:
            raise ValueError("semantic document count mismatch")
        store = DocumentStore(documents_path, None, documents)
    loaded = (state, matrix, store)
    _INDEX_CACHE[cache_key] = loaded
    return loaded


def search(
    index_directory: Path,
    query: str,
    top_k: int,
    base_url: str = DEFAULT_BASE_URL,
    snippet_chars: int = 500,
    unit_types: list[str] | None = None,
    timeout: float = 300.0,
    document_ids: set[str] | None = None,
) -> dict[str, Any]:
    if not query.strip() or top_k < 1 or snippet_chars < 1:
        raise ValueError("query must be non-empty and top_k and snippet_chars must be positive")
    state, matrix, documents = load_index(index_directory)
    installed = model_info(base_url, state["model"]["requested"], timeout=min(timeout, 30.0))
    if installed["digest"] != state["model"]["digest"]:
        raise ValueError("installed Ollama model digest differs from the semantic index")
    query_vector = np.asarray(
        embed_texts(
            base_url,
            installed["resolved"],
            [state["prompts"]["query"].format(query=query)],
            timeout,
        )[0],
        dtype=np.float32,
    )
    if query_vector.shape != (state["matrix"]["dimensions"],):
        raise ValueError("query embedding dimensions differ from the semantic index")
    scores = cosine_scores(matrix, query_vector)
    filtered = bool(unit_types) or document_ids is not None
    if filtered:
        ranked_indices = np.argsort(-scores, kind="stable").tolist()
    else:
        result_count = min(top_k, len(scores))
        if result_count == len(scores):
            selected = np.arange(len(scores))
        else:
            selected = np.argpartition(scores, len(scores) - result_count)[-result_count:]
        ranked_indices = sorted((int(index) for index in selected), key=lambda index: (-float(scores[index]), index))
    results: list[dict[str, Any]] = []
    batch_size = 1024 if filtered else max(len(ranked_indices), 1)
    for start in range(0, len(ranked_indices), batch_size):
        batch_indices = ranked_indices[start:start + batch_size]
        prefetched = documents.get_many(batch_indices)
        for row_index in batch_indices:
            document = prefetched[row_index]
            if unit_types and document["unit_type"] not in unit_types:
                continue
            if document_ids is not None and document["document_id"] not in document_ids:
                continue
            text = document["search_text"]
            results.append({
                "rank": len(results) + 1,
                "score": round(float(scores[row_index]), 8),
                "search_unit_id": document["search_unit_id"],
                "document_id": document["document_id"],
                "unit_type": document["unit_type"],
                "locator": document["locator"],
                "source_evidence_ids": document["source_evidence_ids"],
                "text": text if len(text) <= snippet_chars else text[:snippet_chars] + "…",
            })
            if len(results) >= top_k:
                break
        if len(results) >= top_k:
            break
    return {
        "query": query,
        "method": "cosine-local-embedding",
        "model": state["model"],
        "result_count": len(results),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--query", required=True)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--snippet-chars", type=int, default=500)
    parser.add_argument(
        "--unit-type",
        action="append",
        choices=[
            "paragraph_chunk", "table_row", "slide_text", "page_text", "text_chunk",
            "code_chunk", "notebook_cell", "chart_summary", "chart_series",
        ],
    )
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--intermediate", type=Path, nargs="+",
        help="optional intermediate directories used to add source file paths to results",
    )
    args = parser.parse_args()
    result = search(
        args.index.resolve(),
        args.query,
        args.top_k,
        args.base_url,
        args.snippet_chars,
        args.unit_type,
        args.timeout,
    )
    if args.intermediate:
        sources, _ = load_document_sources(args.intermediate)
        enrich_retrieval(result, sources)
    print(canonical_json(result))


if __name__ == "__main__":
    main()
