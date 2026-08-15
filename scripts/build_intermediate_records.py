#!/usr/bin/env python3
"""Build resumable, streamed intermediate records from source documents.

The builder is question-independent. It recursively discovers supported
office/PDF files, writes per-document shards without retaining all records in
memory, and consolidates them only after every input reaches a terminal state.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from probe_intermediate_records import (
    Probe,
    canonical_json,
    digest_file,
    discover_password_candidates,
    nfc_path,
    stable_id,
)


SUPPORTED_SUFFIXES = {
    ".docx", ".xlsx", ".pptx", ".pdf",
    ".csv", ".tsv", ".json", ".xml", ".ipynb",
    ".md", ".txt", ".py", ".toml", ".yaml", ".yml", ".rst", ".sql", ".sh", ".command",
}
SKIP_DIRECTORY_NAMES = {".git", "__pycache__", ".ipynb_checkpoints", "node_modules"}
EXTRACTOR = "intermediate-record-extractor"
EXTRACTOR_VERSION = "0.5.0"
STATE_VERSION = "1"
STATE_FILE = "build-state.json"
LOCK_FILE = "build.lock"
RECORD_FILES = {
    "documents": "documents.jsonl",
    "evidence": "evidence.jsonl",
    "relations": "relations.jsonl",
}
SKIPPABLE_STATUSES = {"success", "partial", "deferred"}


def discover(root: Path) -> list[Path]:
    return sorted(
        (
            path.resolve() for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in SUPPORTED_SUFFIXES
            and not path.name.startswith("~$")
            and not any(part in SKIP_DIRECTORY_NAMES for part in path.parts)
        ),
        key=lambda path: nfc_path(path.relative_to(root)),
    )


def normalized_relative(root: Path, path: Path) -> str:
    try:
        return nfc_path(path.resolve().relative_to(root.resolve()))
    except ValueError as exc:
        raise ValueError(f"input is outside --root: {path}") from exc


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class BuildLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "BuildLock":
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            raise RuntimeError(f"another build is using this output: {self.path.parent}") from exc
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


class ShardWriter:
    """Write one source document to temporary JSONL files, then rename atomically."""

    def __init__(self, output: Path, document_id: str) -> None:
        self.output = output
        self.document_id = document_id
        self.shard_dir = output / "shards"
        self.shard_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = Path(tempfile.mkdtemp(prefix=f".{document_id}.", dir=self.shard_dir))
        self.handles = {
            kind: (self.temp_dir / file_name).open("w", encoding="utf-8", newline="\n")
            for kind, file_name in RECORD_FILES.items()
        }
        self.counts = {kind: 0 for kind in RECORD_FILES}
        self.last_document: dict[str, Any] | None = None
        self.closed = False

    def emit(self, kind: str, record: dict[str, Any]) -> None:
        if kind not in self.handles:
            raise ValueError(f"unknown record group: {kind}")
        if kind == "documents":
            if self.last_document is not None:
                raise RuntimeError("a document shard may contain only one Document record")
            self.last_document = record
        self.handles[kind].write(canonical_json(record) + "\n")
        self.counts[kind] += 1

    def close(self) -> None:
        if self.closed:
            return
        for handle in self.handles.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        self.closed = True

    def commit(self) -> dict[str, Any]:
        self.close()
        shards: dict[str, dict[str, Any]] = {}
        for kind, file_name in RECORD_FILES.items():
            source = self.temp_dir / file_name
            destination = self.shard_dir / f"{self.document_id}.{kind}.jsonl"
            os.replace(source, destination)
            shards[kind] = {
                "relative_path": nfc_path(destination.relative_to(self.output)),
                "sha256": digest_file(destination),
                "size_bytes": destination.stat().st_size,
                "record_count": self.counts[kind],
            }
        self.temp_dir.rmdir()
        return shards

    def abort(self) -> None:
        for handle in self.handles.values():
            if not handle.closed:
                handle.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.closed = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="source root used for relative paths")
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    parser.add_argument("--input", type=Path, nargs="*", help="optional explicit files; otherwise discover recursively")
    parser.add_argument("--run-at", help="ISO-8601 timestamp; a resumed build reuses the stored value")
    parser.add_argument("--resume", action="store_true", help="resume an existing build-state.json")
    parser.add_argument(
        "--force-input", type=Path, nargs="*", default=[],
        help="with --resume, reprocess these original input files even when their shards are valid",
    )
    parser.add_argument("--fail-fast", action="store_true", help="stop after recording the first failed document")
    parser.add_argument("--max-files", type=int, help="process at most this many pending files, then stop resumably")
    return parser.parse_args()


def validate_inputs(root: Path, paths: list[Path]) -> list[Path]:
    if not paths:
        raise ValueError("no supported input files found")
    unique = sorted({path.resolve() for path in paths}, key=lambda path: normalized_relative(root, path))
    for path in unique:
        if not path.is_file():
            raise ValueError(f"input is not a file: {path}")
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ValueError(f"unsupported explicit input: {path}")
        normalized_relative(root, path)
    return unique


def new_state(root: Path, inputs: list[Path], run_at: str) -> dict[str, Any]:
    return {
        "state_version": STATE_VERSION,
        "build_status": "in_progress",
        "source_root": nfc_path(root),
        "extractor": EXTRACTOR,
        "extractor_version": EXTRACTOR_VERSION,
        "run_at": run_at,
        "input_paths": [normalized_relative(root, path) for path in inputs],
        "entries": {},
    }


def load_state(output: Path, root: Path, explicit_inputs: list[Path] | None) -> tuple[dict[str, Any], list[Path]]:
    state_path = output / STATE_FILE
    if not state_path.is_file():
        raise ValueError(f"cannot resume without {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    expected = {
        "state_version": STATE_VERSION,
        "source_root": nfc_path(root),
        "extractor": EXTRACTOR,
        "extractor_version": EXTRACTOR_VERSION,
    }
    for key, value in expected.items():
        if state.get(key) != value:
            raise ValueError(f"resume mismatch for {key}: {state.get(key)!r} != {value!r}")
    stored_paths = state.get("input_paths", [])
    if explicit_inputs is not None:
        requested = [normalized_relative(root, path) for path in validate_inputs(root, explicit_inputs)]
        if requested != stored_paths:
            raise ValueError("--input must match the original build exactly when using --resume")
    inputs = [root / relative_path for relative_path in stored_paths]
    return state, validate_inputs(root, inputs)


def shard_is_valid(output: Path, shard: dict[str, Any]) -> bool:
    path = output / shard.get("relative_path", "")
    return (
        path.is_file()
        and path.stat().st_size == shard.get("size_bytes")
        and digest_file(path) == shard.get("sha256")
    )


def can_skip(output: Path, entry: dict[str, Any] | None, source_sha256: str) -> bool:
    if not entry or entry.get("source_sha256") != source_sha256:
        return False
    if entry.get("status") not in SKIPPABLE_STATUSES:
        return False
    shards = entry.get("shards", {})
    return set(shards) == set(RECORD_FILES) and all(shard_is_valid(output, shard) for shard in shards.values())


def process_file(
    output: Path,
    root: Path,
    path: Path,
    run_at: str,
    source_sha256: str,
    password_candidates: tuple[str, ...],
) -> tuple[dict[str, Any], Exception | None]:
    relative_path = normalized_relative(root, path)
    document_id = stable_id("doc", {"relative_path": relative_path, "source_sha256": source_sha256})
    writer = ShardWriter(output, document_id)
    extractor = Probe(
        root,
        run_at,
        None,
        diagnostic=False,
        extractor=EXTRACTOR,
        extractor_version=EXTRACTOR_VERSION,
        record_sink=writer.emit,
        retain_records=False,
        password_candidates=password_candidates,
    )
    extraction_error: Exception | None = None
    try:
        extractor.extract(path)
    except Exception as error:
        extraction_error = error
        try:
            extractor.record_failure(path, error)
            extractor.finalize_document()
        except Exception:
            writer.abort()
            raise
    document = writer.last_document
    if document is None:
        writer.abort()
        raise RuntimeError(f"extractor emitted no Document record for {relative_path}")
    if document.get("document_id") != document_id:
        writer.abort()
        raise RuntimeError(f"source identity changed before extraction for {relative_path}")
    if document.get("source", {}).get("sha256") != source_sha256 or digest_file(path) != source_sha256:
        writer.abort()
        raise RuntimeError(f"source changed during extraction for {relative_path}")
    try:
        shards = writer.commit()
    except Exception:
        writer.abort()
        raise

    entry = {
        "relative_path": relative_path,
        "source_sha256": source_sha256,
        "document_id": document_id,
        "status": document["extraction"]["status"],
        "shards": shards,
    }
    return entry, extraction_error


def consolidate(output: Path, state: dict[str, Any]) -> dict[str, int]:
    totals = {kind: 0 for kind in RECORD_FILES}
    entries = state["entries"]
    for kind, file_name in RECORD_FILES.items():
        temporary = output / f".{file_name}.tmp"
        with temporary.open("wb") as destination:
            for relative_path in state["input_paths"]:
                entry = entries[relative_path]
                shard = entry["shards"][kind]
                with (output / shard["relative_path"]).open("rb") as source:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                totals[kind] += shard["record_count"]
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, output / file_name)
    return totals


def main() -> None:
    args = parse_args()
    if args.max_files is not None and args.max_files < 1:
        raise SystemExit("--max-files must be at least 1")
    if args.force_input and not args.resume:
        raise SystemExit("--force-input requires --resume")
    root = args.root.resolve()
    output = args.out.resolve()
    if not root.is_dir():
        raise SystemExit(f"--root is not a directory: {root}")
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise SystemExit("--out must be outside --root to prevent recursive self-ingestion")

    if args.resume:
        if not output.is_dir():
            raise SystemExit(f"resume output is not a directory: {output}")
    else:
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise SystemExit(f"refusing to overwrite non-empty output directory: {output}")
        output.mkdir(parents=True, exist_ok=True)

    with BuildLock(output / LOCK_FILE):
        if args.resume:
            try:
                state, inputs = load_state(output, root, args.input)
            except ValueError as error:
                raise SystemExit(str(error)) from error
            run_at = state["run_at"]
        else:
            try:
                inputs = validate_inputs(root, args.input if args.input is not None else discover(root))
            except ValueError as error:
                raise SystemExit(str(error)) from error
            run_at = args.run_at or datetime.now(timezone.utc).isoformat()
            state = new_state(root, inputs, run_at)
            atomic_json(output / STATE_FILE, state)

        processed_now = 0
        skipped_now = 0
        password_candidates = discover_password_candidates(root)
        forced_paths = {normalized_relative(root, path) for path in args.force_input}
        unknown_forced = forced_paths - set(state["input_paths"])
        if unknown_forced:
            raise SystemExit(f"--force-input was not part of the original build: {sorted(unknown_forced)}")
        for path in inputs:
            relative_path = normalized_relative(root, path)
            source_sha256 = digest_file(path)
            existing = state["entries"].get(relative_path)
            if relative_path not in forced_paths and can_skip(output, existing, source_sha256):
                skipped_now += 1
                continue
            if args.max_files is not None and processed_now >= args.max_files:
                break
            entry, extraction_error = process_file(
                output, root, path, run_at, source_sha256, password_candidates
            )
            state["entries"][relative_path] = entry
            processed_now += 1
            atomic_json(output / STATE_FILE, state)
            if extraction_error is not None and args.fail_fast:
                raise RuntimeError(f"failed to extract {relative_path}: {extraction_error}") from extraction_error

        terminal_paths = {
            relative_path for relative_path, entry in state["entries"].items()
            if entry.get("status") in SKIPPABLE_STATUSES | {"failed"}
        }
        all_reached_terminal = set(state["input_paths"]) <= terminal_paths
        if all_reached_terminal:
            totals = consolidate(output, state)
            total_failures = sum(entry.get("status") == "failed" for entry in state["entries"].values())
            state["build_status"] = "complete_with_failures" if total_failures else "complete"
            state["totals"] = totals
            atomic_json(output / STATE_FILE, state)
        else:
            totals = {kind: sum(
                entry.get("shards", {}).get(kind, {}).get("record_count", 0)
                for entry in state["entries"].values()
            ) for kind in RECORD_FILES}
            state["build_status"] = "in_progress"
            state.pop("totals", None)
            atomic_json(output / STATE_FILE, state)

        print(canonical_json({
            "build_status": state["build_status"],
            "documents": totals["documents"],
            "evidence": totals["evidence"],
            "relations": totals["relations"],
            "failed_documents": sum(entry.get("status") == "failed" for entry in state["entries"].values()),
            "input_files": len(inputs),
            "processed_now": processed_now,
            "skipped_now": skipped_now,
            "output": str(output),
            "extractor": EXTRACTOR,
            "extractor_version": EXTRACTOR_VERSION,
        }))


if __name__ == "__main__":
    main()
