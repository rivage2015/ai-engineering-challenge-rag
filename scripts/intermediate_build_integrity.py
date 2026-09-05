#!/usr/bin/env python3
"""Bind terminal Layer 1 aggregates to their ordered source shards.

This module intentionally validates bytes and state metadata independently of
the record/schema validator.  A syntactically harmless append (for example, a
blank JSONL line) must not be able to pass merely because record iteration
ignores it.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path, PurePosixPath
from typing import Any


MANAGED_EXTRACTOR = "intermediate-record-extractor"
RECORD_FILES = {
    "documents": "documents.jsonl",
    "evidence": "evidence.jsonl",
    "relations": "relations.jsonl",
}
TERMINAL_BUILD_STATUSES = {"complete", "complete_with_failures"}
TERMINAL_ENTRY_STATUSES = {"success", "partial", "deferred", "failed"}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def ordered_shard_manifest_sha256(manifest: list[dict[str, Any]]) -> str:
    """Hash the complete ordered shard manifest, including count and size."""
    return sha256_json(manifest)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"build-state contains duplicate JSON key: {key!r}")
        value[key] = item
    return value


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"build-state contains non-JSON constant: {value}")


def load_build_state(directory: Path) -> dict[str, Any]:
    state_path = directory / "build-state.json"
    try:
        raw = state_path.read_text(encoding="utf-8")
        state = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"cannot read trusted build-state: {state_path}: {exc}") from exc
    if not isinstance(state, dict):
        raise ValueError("build-state must be a JSON object")
    return state


def _required_regular_file(directory: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"{label} relative_path is missing")
    posix = PurePosixPath(relative)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        raise ValueError(f"{label} relative_path is unsafe: {relative!r}")
    path = directory.joinpath(*posix.parts)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} file is missing: {relative}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ValueError(f"{label} must be a regular non-symlink file: {relative}")
    try:
        path.resolve(strict=True).relative_to(directory.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} file escapes the intermediate directory") from exc
    return path


def _file_measurements(path: Path, digest: Any | None = None) -> dict[str, Any]:
    local_digest = hashlib.sha256()
    size_bytes = 0
    record_count = 0
    with path.open("rb") as handle:
        for line in handle:
            local_digest.update(line)
            if digest is not None:
                digest.update(line)
            size_bytes += len(line)
            if line.strip():
                record_count += 1
    return {
        "sha256": local_digest.hexdigest(),
        "size_bytes": size_bytes,
        "record_count": record_count,
    }


def _metadata_contract(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} metadata must be an object")
    required = {"relative_path", "sha256", "size_bytes", "record_count"}
    if not required <= set(value):
        raise ValueError(f"{label} metadata is incomplete")
    if (
        not isinstance(value.get("sha256"), str)
        or len(value["sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in value["sha256"])
        or isinstance(value.get("size_bytes"), bool)
        or not isinstance(value.get("size_bytes"), int)
        or value["size_bytes"] < 0
        or isinstance(value.get("record_count"), bool)
        or not isinstance(value.get("record_count"), int)
        or value["record_count"] < 0
    ):
        raise ValueError(f"{label} metadata has invalid hash, size, or count")
    return value


def _assert_measurements(
    expected: dict[str, Any], actual: dict[str, Any], label: str
) -> None:
    for key in ("sha256", "size_bytes", "record_count"):
        if expected.get(key) != actual[key]:
            raise ValueError(
                f"{label} {key} mismatch: {expected.get(key)!r} != {actual[key]!r}"
            )


def validate_managed_build_integrity(
    directory: Path,
    state: dict[str, Any] | None = None,
) -> dict[str, int] | None:
    """Validate a managed terminal build, or return ``None`` for other producers."""
    directory = directory.resolve(strict=True)
    independently_loaded = load_build_state(directory)
    if state is not None and state != independently_loaded:
        raise ValueError("caller build-state differs from independently loaded state")
    state = independently_loaded
    if state.get("extractor") != MANAGED_EXTRACTOR:
        return None
    if state.get("build_status") not in TERMINAL_BUILD_STATUSES:
        raise ValueError("managed intermediate build has not reached a terminal state")

    input_paths = state.get("input_paths")
    entries = state.get("entries")
    if (
        not isinstance(input_paths, list)
        or not input_paths
        or any(not isinstance(item, str) or not item for item in input_paths)
        or len(input_paths) != len(set(input_paths))
    ):
        raise ValueError("build-state input_paths must be a unique non-empty string list")
    if not isinstance(entries, dict) or set(entries) != set(input_paths):
        raise ValueError("build-state entries do not exactly match input_paths")

    fingerprint = state.get("processing_fingerprint")
    if (
        not isinstance(fingerprint, dict)
        or fingerprint.get("version") != "1"
        or not isinstance(fingerprint.get("payload"), dict)
        or fingerprint.get("sha256") != sha256_json(fingerprint["payload"])
    ):
        raise ValueError("build-state processing fingerprint is missing or invalid")
    fingerprint_sha256 = fingerprint["sha256"]

    aggregate_state = state.get("aggregates")
    if not isinstance(aggregate_state, dict) or set(aggregate_state) != set(RECORD_FILES):
        raise ValueError("build-state aggregate metadata is missing or incomplete")

    totals: dict[str, int] = {}
    for kind, aggregate_name in RECORD_FILES.items():
        ordered_digest = hashlib.sha256()
        ordered_size = 0
        ordered_count = 0
        manifest: list[dict[str, Any]] = []
        for relative_path in input_paths:
            entry = entries[relative_path]
            label = f"entry[{relative_path!r}]"
            if not isinstance(entry, dict):
                raise ValueError(f"{label} must be an object")
            if (
                entry.get("relative_path") != relative_path
                or entry.get("status") not in TERMINAL_ENTRY_STATUSES
            ):
                raise ValueError(f"{label} identity or terminal status is invalid")
            if entry.get("processing_fingerprint_sha256") != fingerprint_sha256:
                raise ValueError(f"{label} has a stale reader fingerprint")
            shards = entry.get("shards")
            if not isinstance(shards, dict) or set(shards) != set(RECORD_FILES):
                raise ValueError(f"{label} shard metadata is incomplete")
            shard = _metadata_contract(shards[kind], f"{label}.{kind}")
            expected_shard_path = (
                f"shards/{entry.get('document_id')}.{kind}.jsonl"
            )
            if shard.get("relative_path") != expected_shard_path:
                raise ValueError(f"{label}.{kind} has a non-canonical shard path")
            shard_path = _required_regular_file(
                directory, shard["relative_path"], f"{label}.{kind}"
            )
            measured = _file_measurements(shard_path, ordered_digest)
            _assert_measurements(shard, measured, f"{label}.{kind}")
            ordered_size += measured["size_bytes"]
            ordered_count += measured["record_count"]
            manifest.append({
                "relative_path": shard["relative_path"],
                "sha256": shard["sha256"],
                "size_bytes": shard["size_bytes"],
                "record_count": shard["record_count"],
            })

        aggregate = _metadata_contract(aggregate_state[kind], f"aggregate.{kind}")
        if aggregate.get("relative_path") != aggregate_name:
            raise ValueError(f"aggregate.{kind} has a non-canonical path")
        expected_manifest_sha = ordered_shard_manifest_sha256(manifest)
        if aggregate.get("ordered_shard_manifest_sha256") != expected_manifest_sha:
            raise ValueError(f"aggregate.{kind} ordered shard manifest hash mismatch")
        reconstructed = {
            "sha256": ordered_digest.hexdigest(),
            "size_bytes": ordered_size,
            "record_count": ordered_count,
        }
        aggregate_path = _required_regular_file(
            directory, aggregate["relative_path"], f"aggregate.{kind}"
        )
        measured_aggregate = _file_measurements(aggregate_path)
        _assert_measurements(aggregate, measured_aggregate, f"aggregate.{kind}")
        _assert_measurements(reconstructed, measured_aggregate, f"aggregate.{kind}.ordered_shards")
        totals[kind] = ordered_count

    if state.get("totals") != totals:
        raise ValueError("build-state totals do not match the ordered aggregate records")
    return totals


__all__ = [
    "MANAGED_EXTRACTOR",
    "ordered_shard_manifest_sha256",
    "validate_managed_build_integrity",
]
