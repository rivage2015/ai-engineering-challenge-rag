#!/usr/bin/env python3
"""Build a resumable, traceable local semantic index from SearchUnits."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from lexical_search_common import canonical_json, digest_file
from ollama_embedding_common import DEFAULT_BASE_URL, DEFAULT_MODEL, embed_texts, model_info


BUILDER = "ollama-semantic-index-builder"
BUILDER_VERSION = "0.2.0"
STATE_FILE = "semantic-index-state.json"
PROGRESS_FILE = "semantic-index-progress.json"
MATRIX_FILE = "semantic-index.npy"
IN_PROGRESS_MATRIX_FILE = ".semantic-index.npy.inprogress"
DOCUMENTS_FILE = "semantic-documents.jsonl"
OFFSETS_FILE = "semantic-document-offsets.npy"
CACHE_FILE = ".embedding-cache.sqlite3"
DOCUMENT_PROMPT = "title: none | text: {text}"
QUERY_PROMPT = "task: search result | query: {query}"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def iter_batches(path: Path, start_index: int, batch_size: int) -> Iterator[tuple[int, list[dict[str, Any]]]]:
    batch: list[dict[str, Any]] = []
    batch_start = start_index
    seen = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            if seen < start_index:
                seen += 1
                continue
            if not batch:
                batch_start = seen
            batch.append(json.loads(line))
            seen += 1
            if len(batch) == batch_size:
                yield batch_start, batch
                batch = []
        if batch:
            yield batch_start, batch


def open_cache(path: Path, model: dict[str, str]) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS embeddings ("
        "text_sha256 TEXT PRIMARY KEY, dimensions INTEGER NOT NULL, vector BLOB NOT NULL)"
    )
    expected = {"model_digest": model["digest"], "normalization": "l2", "dtype": "float32"}
    actual = dict(connection.execute("SELECT key, value FROM metadata"))
    if actual and actual != expected:
        raise ValueError("embedding cache metadata is incompatible with this build")
    if not actual:
        connection.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", expected.items())
        connection.commit()
    return connection


def embed_with_http400_split(
    base_url: str,
    model: str,
    prompts: list[str],
    timeout: float,
) -> list[list[float]]:
    """Retry only oversized/rejected HTTP 400 batches by bisecting them."""
    try:
        return embed_texts(base_url, model, prompts, timeout)
    except RuntimeError as exc:
        if "HTTP 400" not in str(exc) or len(prompts) == 1:
            raise
        middle = len(prompts) // 2
        return (
            embed_with_http400_split(base_url, model, prompts[:middle], timeout)
            + embed_with_http400_split(base_url, model, prompts[middle:], timeout)
        )


def embed_batch(
    connection: sqlite3.Connection,
    units: list[dict[str, Any]],
    base_url: str,
    model: str,
    timeout: float,
    expected_dimensions: int | None,
) -> tuple[np.ndarray, int, int]:
    prompts = [DOCUMENT_PROMPT.format(text=unit["text"]["search_text"]) for unit in units]
    hashes = [hashlib.sha256(prompt.encode("utf-8")).hexdigest() for prompt in prompts]
    unique_prompts: dict[str, str] = {}
    for text_hash, prompt in zip(hashes, prompts):
        unique_prompts.setdefault(text_hash, prompt)
    placeholders = ",".join("?" for _ in unique_prompts)
    cached: dict[str, np.ndarray] = {}
    if placeholders:
        for text_hash, dimensions, blob in connection.execute(
            f"SELECT text_sha256, dimensions, vector FROM embeddings WHERE text_sha256 IN ({placeholders})",
            list(unique_prompts),
        ):
            vector = np.frombuffer(blob, dtype=np.float32)
            if vector.shape != (dimensions,) or (expected_dimensions and dimensions != expected_dimensions):
                raise ValueError("embedding cache contains an incompatible vector")
            cached[text_hash] = vector
    missing_hashes = [text_hash for text_hash in unique_prompts if text_hash not in cached]
    dimensions = expected_dimensions
    if missing_hashes:
        raw = np.asarray(
            embed_with_http400_split(
                base_url,
                model,
                [unique_prompts[text_hash] for text_hash in missing_hashes],
                timeout,
            ),
            dtype=np.float32,
        )
        if raw.ndim != 2 or not np.isfinite(raw).all() or raw.shape[0] != len(missing_hashes):
            raise RuntimeError("embedding matrix is not finite and two-dimensional")
        dimensions = dimensions or int(raw.shape[1])
        if raw.shape[1] != dimensions:
            raise RuntimeError("embedding dimensions changed during the build")
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise RuntimeError("Ollama returned a zero-length embedding")
        normalized = raw / norms
        for text_hash, vector in zip(missing_hashes, normalized):
            cached[text_hash] = vector
            connection.execute(
                "INSERT INTO embeddings(text_sha256, dimensions, vector) VALUES (?, ?, ?)",
                (text_hash, dimensions, sqlite3.Binary(vector.astype(np.float32, copy=False).tobytes())),
            )
        connection.commit()
    if dimensions is None:
        dimensions = next(iter(cached.values())).shape[0]
    matrix = np.stack([cached[text_hash] for text_hash in hashes]).astype(np.float32, copy=False)
    return matrix, int(dimensions), len(missing_hashes)


def write_documents(units_path: Path, output: Path, expected_count: int) -> tuple[Path, Path]:
    documents_path = output / DOCUMENTS_FILE
    offsets_path = output / OFFSETS_FILE
    temporary_documents = output / f".{DOCUMENTS_FILE}.tmp"
    temporary_offsets = output / f".{OFFSETS_FILE}.tmp"
    offsets = np.lib.format.open_memmap(
        temporary_offsets,
        mode="w+",
        dtype=np.int64,
        shape=(expected_count + 1,),
    )
    count = 0
    with units_path.open(encoding="utf-8") as source, temporary_documents.open("wb") as target:
        for line in source:
            if not line.strip():
                continue
            if count >= expected_count:
                raise ValueError("SearchUnit count exceeds search-build-state.json")
            unit = json.loads(line)
            offsets[count] = target.tell()
            record = {
                "search_unit_id": unit["search_unit_id"],
                "document_id": unit["document_id"],
                "unit_type": unit["unit_type"],
                "locator": unit["locator"],
                "source_evidence_ids": unit["source_evidence_ids"],
                "search_text": unit["text"]["search_text"],
            }
            target.write((canonical_json(record) + "\n").encode("utf-8"))
            count += 1
        offsets[count] = target.tell()
        target.flush()
        os.fsync(target.fileno())
    if count != expected_count:
        raise ValueError(f"SearchUnit count mismatch: expected {expected_count}, found {count}")
    offsets.flush()
    del offsets
    os.replace(temporary_documents, documents_path)
    os.replace(temporary_offsets, offsets_path)
    return documents_path, offsets_path


def load_reusable_base(
    base_index: Path,
    units_path: Path,
    record_count: int,
    installed_model: dict[str, str],
) -> tuple[dict[str, Any], np.ndarray, dict[str, Any]]:
    state_path = base_index / STATE_FILE
    state = load_json(state_path)
    if (
        state.get("build_status") != "complete"
        or state.get("normalization") != "l2"
        or state.get("model", {}).get("digest") != installed_model["digest"]
        or state.get("prompts") != {"document": DOCUMENT_PROMPT, "query": QUERY_PROMPT}
    ):
        raise ValueError("base semantic index is incomplete or incompatible")
    matrix_path = base_index / state.get("matrix", {}).get("relative_path", "")
    documents_path = base_index / state.get("documents", {}).get("relative_path", "")
    if digest_file(matrix_path) != state.get("matrix", {}).get("sha256"):
        raise ValueError("base semantic matrix hash mismatch")
    if digest_file(documents_path) != state.get("documents", {}).get("sha256"):
        raise ValueError("base semantic documents hash mismatch")
    base_count = int(state.get("matrix", {}).get("record_count", 0))
    if base_count < 1 or base_count > record_count or state.get("documents", {}).get("record_count") != base_count:
        raise ValueError("base semantic record count cannot be reused")
    matrix = np.load(matrix_path, mmap_mode="r", allow_pickle=False)
    dimensions = int(state.get("matrix", {}).get("dimensions", 0))
    if matrix.shape != (base_count, dimensions) or matrix.dtype != np.float32:
        raise ValueError("base semantic matrix shape or dtype is incompatible")
    compared = 0
    with documents_path.open(encoding="utf-8") as documents, units_path.open(encoding="utf-8") as units:
        while compared < base_count:
            document_line = documents.readline()
            unit_line = units.readline()
            if not document_line or not unit_line:
                raise ValueError("base semantic documents exceed the new SearchUnit prefix")
            if not document_line.strip() or not unit_line.strip():
                continue
            document = json.loads(document_line)
            unit = json.loads(unit_line)
            if (
                document.get("search_unit_id") != unit.get("search_unit_id")
                or document.get("search_text") != unit.get("text", {}).get("search_text")
            ):
                raise ValueError(f"base semantic prefix differs at row {compared}")
            compared += 1
    source = {
        "semantic_state_sha256": digest_file(state_path),
        "matrix_sha256": state["matrix"]["sha256"],
        "documents_sha256": state["documents"]["sha256"],
        "record_count": base_count,
    }
    return state, matrix, source


def build(
    search_output: Path,
    output: Path,
    base_url: str,
    model: str,
    batch_size: int,
    timeout: float,
    resume: bool = False,
    base_index: Path | None = None,
) -> dict[str, Any]:
    if batch_size < 1 or batch_size > 256:
        raise ValueError("batch_size must be between 1 and 256")
    state_path = search_output / "search-build-state.json"
    search_state = load_json(state_path)
    if search_state.get("build_status") != "complete":
        raise ValueError("search unit build must be complete")
    units_path = search_output / search_state.get("output", {}).get("relative_path", "")
    expected_sha = search_state.get("output", {}).get("sha256")
    record_count = int(search_state.get("output", {}).get("record_count", 0))
    if not units_path.is_file() or digest_file(units_path) != expected_sha or record_count < 1:
        raise ValueError("SearchUnit file does not match search-build-state.json")
    installed_model = model_info(base_url, model, timeout=min(timeout, 30.0))
    reusable_base = (
        load_reusable_base(base_index, units_path, record_count, installed_model)
        if base_index is not None else None
    )
    base_source = reusable_base[2] if reusable_base else None
    complete_state_path = output / STATE_FILE
    if complete_state_path.is_file():
        if not resume:
            raise ValueError(f"output directory is not empty: {output}")
        complete = load_json(complete_state_path)
        if (
            complete.get("build_status") == "complete"
            and complete.get("source", {}).get("search_units_sha256") == expected_sha
            and complete.get("model", {}).get("digest") == installed_model["digest"]
        ):
            return complete
        raise ValueError("existing semantic index is incompatible with this resume request")
    if output.exists() and any(output.iterdir()) and not resume:
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    progress_path = output / PROGRESS_FILE
    matrix_path = output / IN_PROGRESS_MATRIX_FILE
    final_matrix_path = output / MATRIX_FILE
    progress: dict[str, Any] | None = load_json(progress_path) if progress_path.is_file() else None
    if progress is None and any(output.iterdir()):
        raise ValueError("resume output has no trustworthy progress state")
    if progress:
        expected_progress = {
            "search_units_sha256": expected_sha,
            "search_state_sha256": digest_file(state_path),
        }
        if (
            progress.get("source") != expected_progress
            or progress.get("model", {}).get("digest") != installed_model["digest"]
            or progress.get("base_index") != base_source
        ):
            raise ValueError("semantic progress state is incompatible with current inputs")
        next_index = int(progress["next_index"])
        dimensions = int(progress["dimensions"])
        embedded_api_records = int(progress.get("embedded_api_records", 0))
        cache_reuses = int(progress.get("cache_reuses", 0))
        reused_base_records = int(progress.get("reused_base_records", 0))
        active_matrix_path = final_matrix_path if final_matrix_path.is_file() else matrix_path
        matrix = np.load(active_matrix_path, mmap_mode="r+", allow_pickle=False)
        if matrix.shape != (record_count, dimensions) or matrix.dtype != np.float32:
            raise ValueError("in-progress semantic matrix is incompatible with progress state")
    else:
        next_index = 0
        dimensions = 0
        embedded_api_records = 0
        cache_reuses = 0
        reused_base_records = 0
        matrix = None
        if reusable_base is not None:
            base_state, base_matrix, _ = reusable_base
            dimensions = int(base_state["matrix"]["dimensions"])
            reused_base_records = int(base_state["matrix"]["record_count"])
            matrix = np.lib.format.open_memmap(
                matrix_path,
                mode="w+",
                dtype=np.float32,
                shape=(record_count, dimensions),
            )
            for start in range(0, reused_base_records, 8192):
                end = min(start + 8192, reused_base_records)
                matrix[start:end] = base_matrix[start:end]
            matrix.flush()
            next_index = reused_base_records
            initial_progress = {
                "state_version": "1",
                "build_status": "in_progress",
                "builder": BUILDER,
                "builder_version": BUILDER_VERSION,
                "source": {
                    "search_units_sha256": expected_sha,
                    "search_state_sha256": digest_file(state_path),
                },
                "base_index": base_source,
                "model": installed_model,
                "record_count": record_count,
                "dimensions": dimensions,
                "next_index": next_index,
                "embedded_api_records": embedded_api_records,
                "cache_reuses": cache_reuses,
                "reused_base_records": reused_base_records,
            }
            atomic_json(progress_path, initial_progress)
    cache = open_cache(output / CACHE_FILE, installed_model)
    try:
        for start, units in iter_batches(units_path, next_index, batch_size):
            batch, batch_dimensions, api_records = embed_batch(
                cache,
                units,
                base_url,
                installed_model["resolved"],
                timeout,
                dimensions or None,
            )
            if matrix is None:
                dimensions = batch_dimensions
                matrix = np.lib.format.open_memmap(
                    matrix_path,
                    mode="w+",
                    dtype=np.float32,
                    shape=(record_count, dimensions),
                )
            if batch_dimensions != dimensions:
                raise RuntimeError("embedding dimensions changed during the build")
            end = start + len(units)
            matrix[start:end] = batch
            matrix.flush()
            embedded_api_records += api_records
            cache_reuses += len(units) - api_records
            next_index = end
            progress = {
                "state_version": "1",
                "build_status": "in_progress",
                "builder": BUILDER,
                "builder_version": BUILDER_VERSION,
                "source": {
                    "search_units_sha256": expected_sha,
                    "search_state_sha256": digest_file(state_path),
                },
                "base_index": base_source,
                "model": installed_model,
                "record_count": record_count,
                "dimensions": dimensions,
                "next_index": next_index,
                "embedded_api_records": embedded_api_records,
                "cache_reuses": cache_reuses,
                "reused_base_records": reused_base_records,
            }
            atomic_json(progress_path, progress)
        if matrix is None or next_index != record_count:
            raise ValueError(f"semantic row count mismatch: expected {record_count}, wrote {next_index}")
        matrix.flush()
        del matrix
        if matrix_path.is_file():
            os.replace(matrix_path, final_matrix_path)
        documents_path, offsets_path = write_documents(units_path, output, record_count)
        result = {
            "state_version": "2",
            "build_status": "complete",
            "builder": BUILDER,
            "builder_version": BUILDER_VERSION,
            "normalization": "l2",
            "prompts": {"document": DOCUMENT_PROMPT, "query": QUERY_PROMPT},
            "model": installed_model,
            "source": {
                "search_units_sha256": expected_sha,
                "search_state_sha256": digest_file(state_path),
                "base_index": base_source,
            },
            "build_statistics": {
                "embedded_api_records": embedded_api_records,
                "cache_reuses": cache_reuses,
                "reused_base_records": reused_base_records,
            },
            "matrix": {
                "relative_path": MATRIX_FILE,
                "sha256": digest_file(final_matrix_path),
                "size_bytes": final_matrix_path.stat().st_size,
                "record_count": record_count,
                "dimensions": dimensions,
                "dtype": "float32",
            },
            "documents": {
                "relative_path": DOCUMENTS_FILE,
                "sha256": digest_file(documents_path),
                "size_bytes": documents_path.stat().st_size,
                "record_count": record_count,
            },
            "document_offsets": {
                "relative_path": OFFSETS_FILE,
                "sha256": digest_file(offsets_path),
                "size_bytes": offsets_path.stat().st_size,
                "record_count": record_count + 1,
                "dtype": "int64",
            },
        }
        atomic_json(complete_state_path, result)
    finally:
        cache.close()
    for suffix in ("", "-wal", "-shm"):
        cache_path = output / f"{CACHE_FILE}{suffix}"
        if cache_path.exists():
            cache_path.unlink()
    if progress_path.exists():
        progress_path.unlink()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search-output", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--base-index",
        type=Path,
        help="reuse an already validated semantic-index prefix and embed only appended SearchUnits",
    )
    args = parser.parse_args()
    print(canonical_json(build(
        args.search_output.resolve(),
        args.out.resolve(),
        args.base_url,
        args.model,
        args.batch_size,
        args.timeout,
        args.resume,
        args.base_index.resolve() if args.base_index else None,
    )))


if __name__ == "__main__":
    main()
