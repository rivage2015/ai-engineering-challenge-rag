#!/usr/bin/env python3
"""Validate lexical index integrity and its SearchUnit source identity."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from lexical_search_common import TOKENIZER, TOKENIZER_VERSION, canonical_json, digest_file


def validate(index_directory: Path, search_output: Path) -> dict[str, Any]:
    state_path = index_directory / "lexical-index-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    database = index_directory / state.get("output", {}).get("relative_path", "")
    search_state_path = search_output / "search-build-state.json"
    search_state = json.loads(search_state_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if state.get("build_status") != "complete" or state.get("deterministic") is not True:
        errors.append("lexical index state is not complete and deterministic")
    if state.get("tokenizer") != TOKENIZER or state.get("tokenizer_version") != TOKENIZER_VERSION:
        errors.append("tokenizer mismatch")
    if not database.is_file():
        errors.append("database is missing")
    else:
        if digest_file(database) != state.get("output", {}).get("sha256"):
            errors.append("database hash mismatch")
        if database.stat().st_size != state.get("output", {}).get("size_bytes"):
            errors.append("database size mismatch")
    if state.get("source", {}).get("search_units_sha256") != search_state.get("output", {}).get("sha256"):
        errors.append("SearchUnit source hash mismatch")
    if state.get("source", {}).get("search_state_sha256") != digest_file(search_state_path):
        errors.append("search build state hash mismatch")
    counts: dict[str, int] = {}
    term_count = 0
    posting_count = 0
    if database.is_file():
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                errors.append(f"SQLite integrity check failed: {integrity}")
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            document_count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            term_count = connection.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
            posting_count = connection.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
            counts = dict(connection.execute("SELECT unit_type, COUNT(*) FROM documents GROUP BY unit_type ORDER BY unit_type"))
            orphan_terms = connection.execute(
                "SELECT COUNT(*) FROM terms WHERE document_frequency != "
                "(SELECT COUNT(*) FROM postings WHERE postings.term_id = terms.term_id)"
            ).fetchone()[0]
            orphan_postings = connection.execute(
                "SELECT COUNT(*) FROM postings LEFT JOIN documents USING(doc_rowid) "
                "WHERE documents.doc_rowid IS NULL"
            ).fetchone()[0]
            if document_count != state.get("output", {}).get("record_count"):
                errors.append("document count mismatch")
            if metadata.get("record_count") != str(document_count):
                errors.append("metadata record count mismatch")
            if metadata.get("tokenizer") != TOKENIZER or metadata.get("tokenizer_version") != TOKENIZER_VERSION:
                errors.append("database tokenizer metadata mismatch")
            if metadata.get("source_search_units_sha256") != state.get("source", {}).get("search_units_sha256"):
                errors.append("database SearchUnit source metadata mismatch")
            if metadata.get("source_search_state_sha256") != state.get("source", {}).get("search_state_sha256"):
                errors.append("database search state source metadata mismatch")
            if counts != state.get("counts_by_type"):
                errors.append("type counts mismatch")
            if orphan_terms or orphan_postings:
                errors.append("posting references or document frequencies are invalid")
        finally:
            connection.close()
    if errors:
        raise ValueError("validation failed:\n- " + "\n- ".join(errors))
    return {
        "documents": state["output"]["record_count"],
        "terms": term_count,
        "postings": posting_count,
        "counts_by_type": counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index_directory", type=Path)
    parser.add_argument("--search-output", required=True, type=Path)
    args = parser.parse_args()
    print(canonical_json({"status": "ok", **validate(args.index_directory.resolve(), args.search_output.resolve())}))


if __name__ == "__main__":
    main()
