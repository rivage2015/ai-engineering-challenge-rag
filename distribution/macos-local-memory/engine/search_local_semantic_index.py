#!/usr/bin/env python3
"""Search a gated local index without passing retrieved text to a chat model."""

from __future__ import annotations

import argparse
import array
import json
import math
import sqlite3
import urllib.request
from pathlib import Path


OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embed"


def embed(model: str, query: str, timeout: int) -> list[float]:
    request = urllib.request.Request(
        OLLAMA_EMBED_URL,
        data=json.dumps({"model": model, "input": query}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    vectors = value.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != 1 or not vectors[0]:
        raise ValueError("query_embedding_missing")
    return [float(item) for item in vectors[0]]


def cosine(left: list[float], right: array.array) -> float:
    if len(left) != len(right):
        raise ValueError("embedding_dimension_mismatch")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--index", required=True)
    parser.add_argument("--purpose", required=True, choices=("safe_answer", "prompt_library"))
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--max-snippet", type=int, default=500)
    args = parser.parse_args()
    if not args.query.strip():
        raise SystemExit("query must not be empty")
    if not 1 <= args.top_k <= 20:
        raise SystemExit("top-k must be between 1 and 20")

    index_path = Path(args.index).resolve(strict=True)
    connection = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    try:
        metadata = {key: json.loads(value) for key, value in connection.execute("SELECT key, value FROM metadata")}
        if metadata.get("content_security_gate") is not True:
            raise ValueError("unsafe_legacy_index_refused")
        if metadata.get("index_purpose") != args.purpose:
            raise ValueError(f"index_purpose_mismatch:{metadata.get('index_purpose')}!={args.purpose}")
        query_vector = embed(metadata["model"], args.query, args.timeout)
        results = []
        for row in connection.execute(
            "SELECT e.evidence_id,e.relative_path,e.locator_json,e.observed_text,v.dimension,v.vector_f32 "
            "FROM evidence e JOIN embeddings v USING(evidence_id)"
        ):
            evidence_id, relative_path, locator_json, observed_text, dimension, blob = row
            vector = array.array("f")
            vector.frombytes(blob)
            if len(vector) != dimension:
                raise ValueError(f"stored_dimension_mismatch:{evidence_id}")
            results.append({
                "score": cosine(query_vector, vector),
                "evidence_id": evidence_id,
                "relative_path": relative_path,
                "locator": json.loads(locator_json),
                "snippet": observed_text[: args.max_snippet],
            })
    finally:
        connection.close()
    results.sort(key=lambda item: (-item["score"], item["relative_path"], item["evidence_id"]))
    print(json.dumps({
        "query": args.query,
        "index_purpose": args.purpose,
        "generation_performed": False,
        "source_text_execution_policy": "never_execute",
        "results": results[: args.top_k],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
