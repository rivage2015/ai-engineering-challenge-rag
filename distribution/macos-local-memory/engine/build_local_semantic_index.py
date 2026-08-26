#!/usr/bin/env python3
"""Build a fully local SQLite semantic index from content Evidence JSONL."""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import os
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path


OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embed"
MAX_EMBED_CHARS = 4_000


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def embed(model: str, texts: list[str], timeout: int) -> list[list[float]]:
    payload = json.dumps({"model": model, "input": texts}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        OLLAMA_EMBED_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    vectors = value.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise ValueError("embedding_count_mismatch")
    if not vectors or not all(isinstance(vector, list) and vector for vector in vectors):
        raise ValueError("empty_embedding")
    dimension = len(vectors[0])
    if any(len(vector) != dimension for vector in vectors):
        raise ValueError("embedding_dimension_mismatch")
    return vectors


def embedding_text(record: dict, relative_path: str) -> tuple[str, bool]:
    locator = record.get("locator", {})
    observed = str(record.get("observed_text", ""))
    prefix = (
        f"ファイル: {relative_path}\n"
        f"場所: {json.dumps(locator, ensure_ascii=False, sort_keys=True)}\n"
        "内容:\n"
    )
    remaining = max(0, MAX_EMBED_CHARS - len(prefix))
    return prefix + observed[:remaining], len(observed) > remaining


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE evidence (
            evidence_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            locator_json TEXT NOT NULL,
            observed_text TEXT NOT NULL,
            embedding_text TEXT NOT NULL,
            embedding_input_truncated INTEGER NOT NULL CHECK (embedding_input_truncated IN (0, 1)),
            observed_sha256 TEXT NOT NULL
        );
        CREATE TABLE embeddings (
            evidence_id TEXT PRIMARY KEY REFERENCES evidence(evidence_id),
            dimension INTEGER NOT NULL,
            vector_f32 BLOB NOT NULL
        );
        CREATE INDEX evidence_document_id_idx ON evidence(document_id);
        CREATE INDEX evidence_relative_path_idx ON evidence(relative_path);
        """
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--documents", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--security-state", required=True)
    parser.add_argument("--index-purpose", required=True, choices=("safe_answer", "prompt_library"))
    parser.add_argument("--model", default="embeddinggemma:latest")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 64:
        raise SystemExit("batch-size must be between 1 and 64")

    evidence_path = Path(args.evidence).resolve(strict=True)
    documents_path = Path(args.documents).resolve(strict=True)
    security_state_path = Path(args.security_state).resolve(strict=True)
    security_state = json.loads(security_state_path.read_text(encoding="utf-8"))
    if security_state.get("classifier") != "deterministic_content_security_gate":
        raise SystemExit("security_state_classifier_invalid")
    if security_state.get("execution_policy") != "never_execute":
        raise SystemExit("security_state_execution_policy_invalid")
    if security_state.get("question_independent") is not True:
        raise SystemExit("security_state_question_independent_invalid")
    evidence_output_name = {
        "safe_answer": "safe-answer-evidence.jsonl",
        "prompt_library": "prompt-library-evidence.jsonl",
    }[args.index_purpose]
    if evidence_path.name != evidence_output_name:
        raise SystemExit(f"security_evidence_filename_mismatch:{evidence_path.name}")
    expected_output = security_state.get("outputs", {}).get(evidence_output_name)
    if not isinstance(expected_output, dict) or expected_output.get("sha256") != sha256_file(evidence_path):
        raise SystemExit("security_evidence_sha256_mismatch")
    if args.index_purpose == "safe_answer" and security_state.get("safe_answer_index_allowed") is not True:
        raise SystemExit("safe_answer_index_not_allowed")
    if args.index_purpose == "prompt_library" and security_state.get("prompt_library_requires_explicit_mode") is not True:
        raise SystemExit("prompt_library_policy_invalid")
    output_path = Path(args.output).resolve(strict=False)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(output_path.name + ".building")
    if temporary_path.exists():
        temporary_path.unlink()

    records = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    documents = [json.loads(line) for line in documents_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    document_paths = {item["document_id"]: item["source"]["relative_path"] for item in documents}
    if len(document_paths) != len(documents):
        raise SystemExit("Document IDs must be unique")
    ids = [record.get("evidence_id") for record in records]
    if len(ids) != len(set(ids)) or any(not isinstance(value, str) or not value for value in ids):
        raise SystemExit("Evidence IDs must be non-empty and unique")

    connection = sqlite3.connect(temporary_path)
    try:
        initialize(connection)
        prepared = []
        for record in records:
            document_id = record.get("document_id")
            if document_id not in document_paths:
                raise ValueError(f"evidence_document_missing:{document_id}")
            relative_path = document_paths[document_id]
            text, truncated = embedding_text(record, relative_path)
            observed = str(record.get("observed_text", ""))
            prepared.append((record, text, truncated, observed, relative_path))

        dimension = None
        for offset in range(0, len(prepared), args.batch_size):
            batch = prepared[offset : offset + args.batch_size]
            vectors = embed(args.model, [item[1] for item in batch], args.timeout)
            for (record, text, truncated, observed, relative_path), vector in zip(batch, vectors, strict=True):
                current_dimension = len(vector)
                if dimension is None:
                    dimension = current_dimension
                elif current_dimension != dimension:
                    raise ValueError("global_embedding_dimension_mismatch")
                evidence_id = record["evidence_id"]
                connection.execute(
                    "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        evidence_id,
                        record["document_id"],
                        relative_path,
                        json.dumps(record.get("locator", {}), ensure_ascii=False, sort_keys=True),
                        observed,
                        text,
                        int(truncated),
                        hashlib.sha256(observed.encode("utf-8")).hexdigest(),
                    ),
                )
                packed = array.array("f", (float(value) for value in vector)).tobytes()
                connection.execute(
                    "INSERT INTO embeddings VALUES (?, ?, ?)",
                    (evidence_id, current_dimension, packed),
                )
            connection.commit()
            print(f"embedded {min(offset + len(batch), len(prepared))}/{len(prepared)}", flush=True)

        metadata = {
            "schema_version": "0.2",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "model": args.model,
            "runtime": "ollama-localhost",
            "evidence_path": str(evidence_path),
            "evidence_sha256": sha256_file(evidence_path),
            "documents_path": str(documents_path),
            "documents_sha256": sha256_file(documents_path),
            "evidence_count": len(records),
            "embedding_dimension": dimension or 0,
            "max_embedding_characters": MAX_EMBED_CHARS,
            "truncated_count": sum(item[2] for item in prepared),
            "external_network_required": False,
            "content_security_gate": True,
            "content_security_policy_version": security_state["policy_version"],
            "content_security_state_path": str(security_state_path),
            "content_security_state_sha256": sha256_file(security_state_path),
            "content_security_execution_policy": "never_execute",
            "index_purpose": args.index_purpose,
            "answer_generation_allowed": args.index_purpose == "safe_answer",
        }
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [(key, json.dumps(value, ensure_ascii=False, sort_keys=True)) for key, value in metadata.items()],
        )
        connection.commit()
        connection.execute("PRAGMA optimize")
        check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise ValueError(f"sqlite_integrity_check:{check}")
    finally:
        connection.close()

    os.replace(temporary_path, output_path)
    print(json.dumps(metadata, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
