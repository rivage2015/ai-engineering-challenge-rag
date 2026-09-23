#!/usr/bin/env python3
"""Freeze validated reading records as one JSON; load without reopening sources.

This is an extraction artifact, not an answer-ready index or a current-version
assertion. The existing text-first adapter gate remains unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Any

from intermediate_build_integrity import (
    MANAGED_EXTRACTOR, RECORD_FILES, load_build_state, validate_managed_build_integrity,
)
from validate_intermediate_records import canonical_json, strict_json_loads
from validate_intermediate_records_streaming import validate_report


MAX_BYTES = 64 * 1024 * 1024
USAGE = {
    "answer_ready": False,
    "source_freshness": "extraction_time_only",
    "complete_source_coverage": False,
}


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_regular(path: Path) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("snapshot input must be a regular file of at most 64 MiB")
        raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("snapshot input exceeds 64 MiB; retain streamed JSONL instead")
        return raw


def _hash_string(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_payload(payload: object) -> None:
    if not isinstance(payload, dict) or set(payload) != {"provenance", "records", "usage"}:
        raise ValueError("invalid snapshot payload fields")
    if canonical_json(payload["usage"]) != canonical_json(USAGE):
        raise ValueError("snapshot must remain extraction-only, not answer-ready")
    records = payload["records"]
    if not isinstance(records, dict) or set(records) != set(RECORD_FILES):
        raise ValueError("snapshot records must contain documents, evidence and relations")
    if any(not isinstance(items, list) for items in records.values()):
        raise ValueError("snapshot record collections must be arrays")
    provenance = payload["provenance"]
    required = {"extractor", "extractor_version", "run_at", "processing_fingerprint",
                "input_manifest", "aggregate_hashes"}
    if not isinstance(provenance, dict) or set(provenance) != required:
        raise ValueError("invalid snapshot provenance fields")
    if provenance["extractor"] != MANAGED_EXTRACTOR:
        raise ValueError("snapshot requires a managed reader")
    for key in ("extractor_version", "run_at"):
        if not isinstance(provenance[key], str) or not provenance[key]:
            raise ValueError(f"invalid provenance {key}")
    fingerprint = provenance["processing_fingerprint"]
    if (not isinstance(fingerprint, dict) or fingerprint.get("version") != "1"
            or not isinstance(fingerprint.get("payload"), dict)
            or fingerprint.get("sha256") != _digest(canonical_json(fingerprint["payload"]).encode())):
        raise ValueError("invalid processing fingerprint")
    policy = fingerprint["payload"].get("reading_policy", "full")
    if policy not in ("full", "text_first_v1"):
        raise ValueError("unknown reading policy")
    hashes = provenance["aggregate_hashes"]
    if (not isinstance(hashes, dict) or set(hashes) != set(RECORD_FILES)
            or not all(_hash_string(value) for value in hashes.values())):
        raise ValueError("invalid original aggregate hashes")

    # Reuse strict schemas, stable IDs, content hashes, references and pending
    # visual coverage checks. No source root is passed: never reopen originals.
    # These temporary JSONL files are NOT a managed/published build generation.
    with tempfile.TemporaryDirectory(prefix="lms-snapshot-validation-") as temporary:
        directory = Path(temporary)
        for kind, filename in RECORD_FILES.items():
            with (directory / filename).open("x", encoding="utf-8") as handle:
                for record in records[kind]:
                    handle.write(canonical_json(record) + "\n")
        report = validate_report(directory, source_root=None, published_schema=True)
        if report.get("status") != "PASS":
            raise ValueError("snapshot record validation did not pass")

    documents = records["documents"]
    by_id = {doc["document_id"]: doc for doc in documents}
    manifest = provenance["input_manifest"]
    if not isinstance(manifest, list) or not manifest or len(manifest) != len(documents):
        raise ValueError("snapshot input manifest differs from documents")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    fields = {"relative_path", "document_id", "source_sha256", "size_bytes", "status"}
    for entry in manifest:
        if not isinstance(entry, dict) or set(entry) != fields:
            raise ValueError("invalid snapshot source entry")
        relative = entry["relative_path"]
        if (not isinstance(relative, str) or not relative
                or PurePosixPath(relative).is_absolute()
                or any(part in ("", ".", "..") for part in relative.split("/"))):
            raise ValueError("unsafe snapshot relative source path")
        identity = entry["document_id"]
        if not isinstance(identity, str) or identity not in by_id:
            raise ValueError("snapshot source document is missing")
        if identity in seen_ids or relative in seen_paths:
            raise ValueError("duplicate snapshot source identity")
        seen_ids.add(identity); seen_paths.add(relative)
        doc = by_id[identity]
        source, extraction = doc["source"], doc["extraction"]
        if (source["relative_path"] != relative or source["sha256"] != entry["source_sha256"]
                or type(entry["size_bytes"]) is not int or source["size_bytes"] != entry["size_bytes"]
                or extraction["status"] != entry["status"]):
            raise ValueError("snapshot manifest/source binding mismatch")
        if extraction.get("reading_policy", "full") != policy:
            raise ValueError("snapshot reading policy differs from document")


def load_snapshot(path: Path) -> dict[str, Any]:
    """Read and validate only the frozen JSON, even if originals are offline."""
    envelope = strict_json_loads(_read_regular(path).decode("utf-8"))
    keys = {"schema_version", "record_type", "snapshot_id", "payload_sha256", "payload"}
    if not isinstance(envelope, dict) or set(envelope) != keys:
        raise ValueError("invalid reading snapshot envelope")
    if envelope["schema_version"] != "1" or envelope["record_type"] != "reading_snapshot":
        raise ValueError("unsupported reading snapshot version/type")
    digest = _digest(canonical_json(envelope["payload"]).encode("utf-8"))
    if envelope["payload_sha256"] != digest or envelope["snapshot_id"] != f"reading_{digest}":
        raise ValueError("reading snapshot content hash mismatch")
    _validate_payload(envelope["payload"])
    return envelope


def freeze(intermediate: Path, output: Path) -> dict[str, Any]:
    """Publish once without overwriting, modifying input, or running extraction."""
    if os.path.lexists(output):
        raise ValueError("refusing to overwrite an existing snapshot")
    intermediate = intermediate.resolve(strict=True)
    try:
        output.resolve().relative_to(intermediate)
    except ValueError:
        pass
    else:
        raise ValueError("snapshot output must be outside the intermediate input")
    state = load_build_state(intermediate)
    if validate_managed_build_integrity(intermediate, state) is None:
        raise ValueError("snapshot requires a managed terminal build")
    records, original_bytes = {}, {}
    for kind, filename in RECORD_FILES.items():
        raw = _read_regular(intermediate / filename)
        expected = state["aggregates"][kind]
        if _digest(raw) != expected["sha256"] or len(raw) != expected["size_bytes"]:
            raise ValueError("intermediate changed while freezing")
        items = [strict_json_loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
        if len(items) != expected["record_count"]:
            raise ValueError("intermediate count differs from build state")
        original_bytes[kind] = raw
        records[kind] = items
    by_id = {doc["document_id"]: doc for doc in records["documents"]}
    manifest = []
    for relative in state["input_paths"]:
        entry = state["entries"][relative]
        if entry["document_id"] not in by_id:
            raise ValueError("build entry references a missing document")
        doc = by_id[entry["document_id"]]
        manifest.append({
            "relative_path": relative, "document_id": entry["document_id"],
            "source_sha256": entry["source_sha256"], "size_bytes": doc["source"]["size_bytes"],
            "status": entry["status"],
        })
    payload = {
        "provenance": {
            "extractor": state["extractor"], "extractor_version": state["extractor_version"],
            "run_at": state["run_at"], "processing_fingerprint": state["processing_fingerprint"],
            "input_manifest": manifest,
            "aggregate_hashes": {kind: _digest(raw) for kind, raw in original_bytes.items()},
        },
        "records": records, "usage": dict(USAGE),
    }
    _validate_payload(payload)
    validate_managed_build_integrity(intermediate, state)
    for kind, filename in RECORD_FILES.items():
        if _read_regular(intermediate / filename) != original_bytes[kind]:
            raise ValueError("intermediate changed before snapshot publication")
    digest = _digest(canonical_json(payload).encode("utf-8"))
    envelope = {"schema_version": "1", "record_type": "reading_snapshot",
                "snapshot_id": f"reading_{digest}", "payload_sha256": digest, "payload": payload}
    raw = (json.dumps(envelope, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("single JSON exceeds 64 MiB; retain streamed JSONL instead")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o400)
        # A hard link publishes atomically and fails if a file/symlink appeared.
        os.link(temporary, output)
    finally:
        os.unlink(temporary)  # Only our own mkstemp file, never an existing output.
    return envelope


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("intermediate", type=Path)
    export.add_argument("output", type=Path)
    check = commands.add_parser("check")
    check.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    result = freeze(args.intermediate, args.output) if args.command == "export" else load_snapshot(args.snapshot)
    print(json.dumps({"snapshot_id": result["snapshot_id"],
                      "counts": {key: len(value) for key, value in result["payload"]["records"].items()},
                      "usage": result["payload"]["usage"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
