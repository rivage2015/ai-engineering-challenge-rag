#!/usr/bin/env python3
"""Search an API-free SQLite index with BM25 and return traceable evidence."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from lexical_search_common import TOKENIZER, TOKENIZER_VERSION, canonical_json, tokenize


def search(
    index_directory: Path,
    query: str,
    top_k: int,
    unit_types: list[str] | None = None,
    snippet_chars: int = 500,
    k1: float = 1.2,
    b: float = 0.75,
) -> dict[str, Any]:
    if not query.strip():
        raise ValueError("query must not be empty")
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if snippet_chars < 1:
        raise ValueError("snippet_chars must be at least 1")
    query_terms = sorted(set(tokenize(query)))
    if not query_terms:
        raise ValueError("query has no indexable tokens")
    if len(query_terms) > 500:
        raise ValueError("query is too long: at most 500 unique terms are supported")
    database = index_directory / "lexical-index.sqlite3"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        metadata = {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM metadata")}
        if metadata.get("tokenizer") != TOKENIZER or metadata.get("tokenizer_version") != TOKENIZER_VERSION:
            raise ValueError("index tokenizer is incompatible with this search script")
        document_count = int(metadata["record_count"])
        average_length = float(metadata["average_document_length"])
        placeholders = ",".join("?" for _ in query_terms)
        rows = connection.execute(
            f"SELECT terms.term_id, terms.term, terms.document_frequency, "
            f"postings.doc_rowid, postings.term_frequency, documents.document_length "
            f"FROM terms JOIN postings USING(term_id) JOIN documents USING(doc_rowid) "
            f"WHERE terms.term IN ({placeholders})",
            query_terms,
        )
        scores: dict[int, float] = defaultdict(float)
        matched_terms: dict[int, set[str]] = defaultdict(set)
        for row in rows:
            document_frequency = row["document_frequency"]
            inverse_document_frequency = math.log(1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5))
            term_frequency = row["term_frequency"]
            length_ratio = row["document_length"] / average_length if average_length else 0.0
            denominator = term_frequency + k1 * (1.0 - b + b * length_ratio)
            scores[row["doc_rowid"]] += inverse_document_frequency * term_frequency * (k1 + 1.0) / denominator
            matched_terms[row["doc_rowid"]].add(row["term"])
        ranked_ids = sorted(scores, key=lambda doc_rowid: (-scores[doc_rowid], doc_rowid))
        results: list[dict[str, Any]] = []
        for doc_rowid in ranked_ids:
            row = connection.execute("SELECT * FROM documents WHERE doc_rowid = ?", (doc_rowid,)).fetchone()
            if unit_types and row["unit_type"] not in unit_types:
                continue
            text = row["search_text"]
            results.append({
                "rank": len(results) + 1,
                "score": round(scores[doc_rowid], 8),
                "matched_query_terms": len(matched_terms[doc_rowid]),
                "query_term_count": len(query_terms),
                "search_unit_id": row["search_unit_id"],
                "document_id": row["document_id"],
                "unit_type": row["unit_type"],
                "locator": json.loads(row["locator_json"]),
                "source_evidence_ids": json.loads(row["source_evidence_ids_json"]),
                "text": text if len(text) <= snippet_chars else text[:snippet_chars] + "…",
            })
            if len(results) >= top_k:
                break
        return {
            "query": query,
            "method": "BM25",
            "tokenizer": TOKENIZER,
            "tokenizer_version": TOKENIZER_VERSION,
            "query_term_count": len(query_terms),
            "result_count": len(results),
            "results": results,
        }
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--query", required=True)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--unit-type", action="append", choices=["paragraph_chunk", "table_row", "slide_text", "page_text"])
    parser.add_argument("--snippet-chars", type=int, default=500)
    args = parser.parse_args()
    print(canonical_json(search(args.index.resolve(), args.query, args.top_k, args.unit_type, args.snippet_chars)))


if __name__ == "__main__":
    main()
