#!/usr/bin/env python3
"""Fuse traceable lexical and local semantic retrieval with weighted RRF."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from lexical_search_common import canonical_json
from ollama_embedding_common import DEFAULT_BASE_URL
from search_lexical_index import search as search_lexical
from search_semantic_index import search as search_semantic


FUSER = "weighted-reciprocal-rank-fusion"
FUSER_VERSION = "0.1.0"


def search(
    lexical_index: Path,
    semantic_index: Path,
    query: str,
    top_k: int,
    base_url: str = DEFAULT_BASE_URL,
    candidate_k: int = 50,
    rrf_k: float = 60.0,
    lexical_weight: float = 1.0,
    semantic_weight: float = 0.25,
    snippet_chars: int = 500,
    adaptive_semantic: bool = True,
    low_coverage_threshold: float = 0.25,
    low_margin_threshold: float = 1.4,
    low_confidence_semantic_weight: float = 1.0,
) -> dict[str, Any]:
    if top_k < 1 or candidate_k < top_k:
        raise ValueError("candidate_k must be at least top_k and both must be positive")
    if (
        rrf_k <= 0
        or lexical_weight < 0
        or semantic_weight < 0
        or low_confidence_semantic_weight < 0
        or not 0 <= low_coverage_threshold <= 1
        or low_margin_threshold <= 0
    ):
        raise ValueError("invalid RRF weight or adaptive confidence threshold")
    lexical = search_lexical(lexical_index, query, candidate_k, snippet_chars=snippet_chars)
    semantic = search_semantic(semantic_index, query, candidate_k, base_url, snippet_chars)
    lexical_coverage = 0.0
    lexical_margin = float("inf")
    if lexical["results"]:
        first = lexical["results"][0]
        lexical_coverage = first["matched_query_terms"] / max(first["query_term_count"], 1)
        if len(lexical["results"]) > 1:
            lexical_margin = first["score"] / max(lexical["results"][1]["score"], 1e-12)
    low_lexical_confidence = adaptive_semantic and (
        not lexical["results"]
        or (
            lexical_coverage < low_coverage_threshold
            and lexical_margin < low_margin_threshold
        )
    )
    effective_semantic_weight = (
        max(semantic_weight, low_confidence_semantic_weight)
        if low_lexical_confidence else semantic_weight
    )
    combined: dict[str, dict[str, Any]] = {}
    for source, weight in ((lexical, lexical_weight), (semantic, effective_semantic_weight)):
        source_name = "lexical" if source is lexical else "semantic"
        for item in source["results"]:
            search_unit_id = item["search_unit_id"]
            entry = combined.setdefault(search_unit_id, {
                "search_unit_id": search_unit_id,
                "document_id": item["document_id"],
                "unit_type": item["unit_type"],
                "locator": item["locator"],
                "source_evidence_ids": item["source_evidence_ids"],
                "text": item["text"],
                "score": 0.0,
                "lexical_rank": None,
                "semantic_rank": None,
            })
            entry["score"] += weight / (rrf_k + item["rank"])
            entry[f"{source_name}_rank"] = item["rank"]
            entry[f"{source_name}_score"] = item["score"]
    ranked = sorted(combined.values(), key=lambda item: (-item["score"], item["search_unit_id"]))
    results = []
    for rank, item in enumerate(ranked[:top_k], 1):
        item = dict(item)
        item["rank"] = rank
        item["score"] = round(item["score"], 10)
        results.append(item)
    return {
        "query": query,
        "method": "BM25-field-parent+local-semantic-RRF",
        "fuser": FUSER,
        "fuser_version": FUSER_VERSION,
        "parameters": {
            "candidate_k": candidate_k,
            "rrf_k": rrf_k,
            "lexical_weight": lexical_weight,
            "semantic_weight": semantic_weight,
            "adaptive_semantic": adaptive_semantic,
            "effective_semantic_weight": effective_semantic_weight,
            "low_coverage_threshold": low_coverage_threshold,
            "low_margin_threshold": low_margin_threshold,
        },
        "lexical_confidence": {
            "matched_term_coverage": round(lexical_coverage, 8),
            "top_score_margin": round(lexical_margin, 8) if lexical_margin != float("inf") else None,
            "low_confidence": low_lexical_confidence,
        },
        "result_count": len(results),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lexical-index", required=True, type=Path)
    parser.add_argument("--semantic-index", required=True, type=Path)
    parser.add_argument("--query", required=True)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--rrf-k", type=float, default=60.0)
    parser.add_argument("--lexical-weight", type=float, default=1.0)
    parser.add_argument("--semantic-weight", type=float, default=0.25)
    parser.add_argument("--snippet-chars", type=int, default=500)
    parser.add_argument("--no-adaptive-semantic", action="store_true")
    args = parser.parse_args()
    print(canonical_json(search(
        args.lexical_index.resolve(), args.semantic_index.resolve(), args.query,
        args.top_k, args.base_url, args.candidate_k, args.rrf_k,
        args.lexical_weight, args.semantic_weight, args.snippet_chars,
        not args.no_adaptive_semantic,
    )))


if __name__ == "__main__":
    main()
