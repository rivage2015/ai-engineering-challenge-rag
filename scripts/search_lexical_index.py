#!/usr/bin/env python3
"""Search an API-free SQLite index with BM25 and return traceable evidence."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

from lexical_search_common import TOKENIZER, TOKENIZER_VERSION, canonical_json, tokenize


RANKER = "field-aware-parent-child-reranker"
RANKER_VERSION = "0.1.0"


def normalize_match_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if not character.isspace())


def exact_field_values(search_text: str, query: str) -> list[str]:
    """Return short labelled values that occur verbatim in the query.

    SearchUnit table rows use one ``label: value`` pair per line.  This rule is
    independent of the label language and document; it only rewards an exact
    value mention and does not contain task-specific synonyms or answer rules.
    """
    normalized_query = normalize_match_text(query)
    matches: list[str] = []
    for line in search_text.splitlines():
        if ":" not in line:
            continue
        _, value = line.split(":", 1)
        value = value.strip()
        normalized_value = normalize_match_text(value)
        if 2 <= len(normalized_value) <= 64 and normalized_value in normalized_query:
            matches.append(value)
    return list(dict.fromkeys(matches))


def search(
    index_directory: Path,
    query: str,
    top_k: int,
    unit_types: list[str] | None = None,
    snippet_chars: int = 500,
    k1: float = 1.2,
    b: float = 0.75,
    field_value_weight: float = 0.5,
    parent_context_penalty: float = 2.0,
) -> dict[str, Any]:
    if not query.strip():
        raise ValueError("query must not be empty")
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if snippet_chars < 1:
        raise ValueError("snippet_chars must be at least 1")
    if field_value_weight < 0.0:
        raise ValueError("field_value_weight must not be negative")
    if parent_context_penalty < 0.0:
        raise ValueError("parent_context_penalty must not be negative")
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
        inverse_document_frequencies: dict[str, float] = {}
        for row in rows:
            document_frequency = row["document_frequency"]
            inverse_document_frequency = math.log(1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5))
            inverse_document_frequencies[row["term"]] = inverse_document_frequency
            term_frequency = row["term_frequency"]
            length_ratio = row["document_length"] / average_length if average_length else 0.0
            denominator = term_frequency + k1 * (1.0 - b + b * length_ratio)
            scores[row["doc_rowid"]] += inverse_document_frequency * term_frequency * (k1 + 1.0) / denominator
            matched_terms[row["doc_rowid"]].add(row["term"])
        lexical_ranked_ids = sorted(scores, key=lambda doc_rowid: (-scores[doc_rowid], doc_rowid))
        candidate_limit = max(50, top_k * 5)
        rows_by_id: dict[int, sqlite3.Row] = {}
        candidate_ids: list[int] = []
        for doc_rowid in lexical_ranked_ids:
            row = connection.execute("SELECT * FROM documents WHERE doc_rowid = ?", (doc_rowid,)).fetchone()
            if unit_types and row["unit_type"] not in unit_types:
                continue
            rows_by_id[doc_rowid] = row
            candidate_ids.append(doc_rowid)
            if len(candidate_ids) >= candidate_limit:
                break
        field_value_bonuses: dict[int, float] = {}
        exact_matches: dict[int, list[str]] = {}
        source_sets: dict[int, set[str]] = {}
        for doc_rowid in candidate_ids:
            row = rows_by_id[doc_rowid]
            matches = exact_field_values(row["search_text"], query)
            exact_matches[doc_rowid] = matches
            source_sets[doc_rowid] = set(json.loads(row["source_evidence_ids_json"]))
            matched_value_terms = {
                term
                for value in matches
                for term in tokenize(value)
                if term in query_terms
            }
            field_value_bonuses[doc_rowid] = field_value_weight * sum(
                inverse_document_frequencies.get(term, 0.0) for term in matched_value_terms
            )
        ancestor_penalties: dict[int, float] = {doc_rowid: 0.0 for doc_rowid in candidate_ids}
        matched_descendants = [doc_rowid for doc_rowid in candidate_ids if exact_matches[doc_rowid]]
        for ancestor_id in candidate_ids:
            ancestor_row = rows_by_id[ancestor_id]
            ancestor_sources = source_sets[ancestor_id]
            for descendant_id in matched_descendants:
                if ancestor_id == descendant_id:
                    continue
                descendant_row = rows_by_id[descendant_id]
                if (
                    ancestor_row["document_id"] == descendant_row["document_id"]
                    and ancestor_sources < source_sets[descendant_id]
                ):
                    ancestor_penalties[ancestor_id] = parent_context_penalty
                    break
        final_scores = {
            doc_rowid: scores[doc_rowid] + field_value_bonuses[doc_rowid] - ancestor_penalties[doc_rowid]
            for doc_rowid in candidate_ids
        }
        ranked_ids = sorted(final_scores, key=lambda doc_rowid: (-final_scores[doc_rowid], doc_rowid))
        results: list[dict[str, Any]] = []
        for doc_rowid in ranked_ids:
            row = rows_by_id[doc_rowid]
            text = row["search_text"]
            results.append({
                "rank": len(results) + 1,
                "score": round(final_scores[doc_rowid], 8),
                "lexical_score": round(scores[doc_rowid], 8),
                "field_value_bonus": round(field_value_bonuses[doc_rowid], 8),
                "parent_context_penalty": round(ancestor_penalties[doc_rowid], 8),
                "exact_field_value_matches": exact_matches[doc_rowid],
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
            "method": "BM25+field-aware-parent-child" if field_value_weight or parent_context_penalty else "BM25",
            "ranker": RANKER,
            "ranker_version": RANKER_VERSION,
            "field_value_weight": field_value_weight,
            "parent_context_penalty": parent_context_penalty,
            "candidate_pool_size": len(candidate_ids),
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
    parser.add_argument("--field-value-weight", type=float, default=0.5)
    parser.add_argument("--parent-context-penalty", type=float, default=2.0)
    args = parser.parse_args()
    print(canonical_json(search(
        args.index.resolve(), args.query, args.top_k, args.unit_type, args.snippet_chars,
        field_value_weight=args.field_value_weight,
        parent_context_penalty=args.parent_context_penalty,
    )))


if __name__ == "__main__":
    main()
