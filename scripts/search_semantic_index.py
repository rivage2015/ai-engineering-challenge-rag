#!/usr/bin/env python3
"""Search a local normalized embedding matrix with cosine similarity."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from lexical_search_common import canonical_json, digest_file
from ollama_embedding_common import DEFAULT_BASE_URL, embed_texts, model_info


def load_index(index_directory: Path) -> tuple[dict[str, Any], np.ndarray, list[dict[str, Any]]]:
    state_path = index_directory / "semantic-index-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    matrix_path = index_directory / state["matrix"]["relative_path"]
    documents_path = index_directory / state["documents"]["relative_path"]
    if digest_file(matrix_path) != state["matrix"]["sha256"]:
        raise ValueError("semantic matrix hash mismatch")
    if digest_file(documents_path) != state["documents"]["sha256"]:
        raise ValueError("semantic documents hash mismatch")
    matrix = np.load(matrix_path, mmap_mode="r", allow_pickle=False)
    documents = [json.loads(line) for line in documents_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if matrix.shape != (len(documents), state["matrix"]["dimensions"]):
        raise ValueError("semantic index shape mismatch")
    return state, matrix, documents


def search(
    index_directory: Path,
    query: str,
    top_k: int,
    base_url: str = DEFAULT_BASE_URL,
    snippet_chars: int = 500,
    unit_types: list[str] | None = None,
    timeout: float = 300.0,
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
    norm = float(np.linalg.norm(query_vector))
    if not norm:
        raise ValueError("query embedding has zero length")
    scores = matrix @ (query_vector / norm)
    ranked = np.argsort(-scores, kind="stable")
    results: list[dict[str, Any]] = []
    for row_index in ranked:
        document = documents[int(row_index)]
        if unit_types and document["unit_type"] not in unit_types:
            continue
        text = document["search_text"]
        results.append({
            "rank": len(results) + 1,
            "score": round(float(scores[int(row_index)]), 8),
            "search_unit_id": document["search_unit_id"],
            "document_id": document["document_id"],
            "unit_type": document["unit_type"],
            "locator": document["locator"],
            "source_evidence_ids": document["source_evidence_ids"],
            "text": text if len(text) <= snippet_chars else text[:snippet_chars] + "…",
        })
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
    parser.add_argument("--unit-type", action="append", choices=["paragraph_chunk", "table_row", "slide_text", "page_text"])
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()
    print(canonical_json(search(
        args.index.resolve(), args.query, args.top_k, args.base_url,
        args.snippet_chars, args.unit_type, args.timeout,
    )))


if __name__ == "__main__":
    main()
