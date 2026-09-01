#!/usr/bin/env python3
"""Build an API-free SQLite BM25 index from SearchUnit JSONL."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from lexical_search_common import (
    TOKENIZER,
    TOKENIZER_VERSION,
    canonical_json,
    digest_file,
    term_frequencies,
)


INDEXER = "sqlite-bm25-index-builder"
INDEXER_VERSION = "0.2.0"
STATE_FILE = "lexical-index-state.json"
DATABASE_FILE = "lexical-index.sqlite3"
PROVISIONAL_OCR_MARKER = "[暫定読取]"


def indexable_search_text(unit: dict[str, Any]) -> str:
    """Remove the audit label, never the provisional OCR observation itself.

    The canonical marker remains in stored/search-result text and therefore at
    the answer boundary. Excluding its repeated presentation token from BM25
    prevents a multi-line provisional packet from receiving an artificial
    document-length penalty merely because every line is visibly labelled.
    """
    text = unit["text"]["search_text"]
    context = unit.get("context", {})
    if (
        unit.get("unit_type") != "image_text_packet"
        or context.get("quality_tier") != "provisional"
        or context.get("provisional_marker") != PROVISIONAL_OCR_MARKER
    ):
        return text
    prefix = PROVISIONAL_OCR_MARKER + " "
    return "\n".join(
        line[len(prefix):] if line.lstrip().startswith(prefix) and line == line.lstrip() else line
        for line in text.splitlines()
    )


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON: {path}: {exc}") from exc


def prepare_output(output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=FILE;
        CREATE TABLE documents (
            doc_rowid INTEGER PRIMARY KEY,
            search_unit_id TEXT NOT NULL UNIQUE,
            document_id TEXT NOT NULL,
            unit_type TEXT NOT NULL,
            locator_json TEXT NOT NULL,
            source_evidence_ids_json TEXT NOT NULL,
            search_text TEXT NOT NULL,
            document_length INTEGER NOT NULL CHECK(document_length > 0)
        );
        CREATE TABLE staging_postings (
            term TEXT NOT NULL,
            doc_rowid INTEGER NOT NULL,
            term_frequency INTEGER NOT NULL CHECK(term_frequency > 0)
        );
        CREATE TABLE terms (
            term_id INTEGER PRIMARY KEY,
            term TEXT NOT NULL UNIQUE,
            document_frequency INTEGER NOT NULL CHECK(document_frequency > 0)
        );
        CREATE TABLE postings (
            term_id INTEGER NOT NULL,
            doc_rowid INTEGER NOT NULL,
            term_frequency INTEGER NOT NULL CHECK(term_frequency > 0),
            PRIMARY KEY (term_id, doc_rowid),
            FOREIGN KEY (term_id) REFERENCES terms(term_id),
            FOREIGN KEY (doc_rowid) REFERENCES documents(doc_rowid)
        ) WITHOUT ROWID;
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)


def build(search_output: Path, output: Path) -> dict[str, Any]:
    search_state_path = search_output / "search-build-state.json"
    search_state = load_json(search_state_path)
    if search_state.get("build_status") != "complete":
        raise ValueError("search unit build must be complete")
    units_path = search_output / search_state.get("output", {}).get("relative_path", "")
    expected_sha = search_state.get("output", {}).get("sha256")
    if not units_path.is_file() or digest_file(units_path) != expected_sha:
        raise ValueError("SearchUnit file does not match search-build-state.json")
    prepare_output(output)
    temporary_database = output / f".{DATABASE_FILE}.tmp"
    final_database = output / DATABASE_FILE
    connection = sqlite3.connect(temporary_database)
    counts_by_type: dict[str, int] = {}
    total_length = 0
    record_count = 0
    try:
        initialize(connection)
        with units_path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    unit = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{units_path}:{line_number}: invalid JSON: {exc}") from exc
                frequencies = term_frequencies(indexable_search_text(unit))
                if not frequencies:
                    raise ValueError(f"{units_path}:{line_number}: no indexable tokens")
                record_count += 1
                document_length = sum(frequencies.values())
                total_length += document_length
                unit_type = unit["unit_type"]
                counts_by_type[unit_type] = counts_by_type.get(unit_type, 0) + 1
                connection.execute(
                    "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record_count,
                        unit["search_unit_id"],
                        unit["document_id"],
                        unit_type,
                        canonical_json(unit["locator"]),
                        canonical_json(unit["source_evidence_ids"]),
                        unit["text"]["search_text"],
                        document_length,
                    ),
                )
                connection.executemany(
                    "INSERT INTO staging_postings VALUES (?, ?, ?)",
                    ((term, record_count, frequency) for term, frequency in sorted(frequencies.items())),
                )
        if record_count != search_state.get("output", {}).get("record_count"):
            raise ValueError("SearchUnit record count does not match search-build-state.json")
        connection.execute(
            "INSERT INTO terms(term, document_frequency) "
            "SELECT term, COUNT(*) FROM staging_postings GROUP BY term ORDER BY term"
        )
        connection.execute(
            "INSERT INTO postings(term_id, doc_rowid, term_frequency) "
            "SELECT terms.term_id, staging_postings.doc_rowid, staging_postings.term_frequency "
            "FROM staging_postings JOIN terms USING(term) ORDER BY terms.term_id, staging_postings.doc_rowid"
        )
        connection.execute("DROP TABLE staging_postings")
        connection.executescript("""
            CREATE INDEX postings_doc_idx ON postings(doc_rowid);
            CREATE INDEX documents_type_idx ON documents(unit_type);
        """)
        metadata = {
            "indexer": INDEXER,
            "indexer_version": INDEXER_VERSION,
            "tokenizer": TOKENIZER,
            "tokenizer_version": TOKENIZER_VERSION,
            "source_search_units_sha256": expected_sha,
            "source_search_state_sha256": digest_file(search_state_path),
            "record_count": str(record_count),
            "total_document_length": str(total_length),
            "average_document_length": repr(total_length / record_count if record_count else 0.0),
        }
        connection.executemany("INSERT INTO metadata VALUES (?, ?)", sorted(metadata.items()))
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")
        connection.execute("VACUUM")
    except Exception:
        connection.close()
        temporary_database.unlink(missing_ok=True)
        raise
    connection.close()
    os.replace(temporary_database, final_database)
    result = {
        "state_version": "1",
        "build_status": "complete",
        "indexer": INDEXER,
        "indexer_version": INDEXER_VERSION,
        "tokenizer": TOKENIZER,
        "tokenizer_version": TOKENIZER_VERSION,
        "deterministic": True,
        "source": {
            "search_units_sha256": expected_sha,
            "search_state_sha256": digest_file(search_state_path),
        },
        "output": {
            "relative_path": DATABASE_FILE,
            "sha256": digest_file(final_database),
            "size_bytes": final_database.stat().st_size,
            "record_count": record_count,
        },
        "counts_by_type": dict(sorted(counts_by_type.items())),
        "total_document_length": total_length,
        "average_document_length": total_length / record_count if record_count else 0.0,
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
    args = parser.parse_args()
    print(canonical_json(build(args.search_output.resolve(), args.out.resolve())))


if __name__ == "__main__":
    main()
