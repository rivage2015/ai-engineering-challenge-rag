#!/usr/bin/env python3
"""Expand a frozen reading snapshot without pretending to run the reader again."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import unicodedata

from freeze_reading_snapshot import _read_regular, load_snapshot
from intermediate_build_integrity import RECORD_FILES, ordered_shard_manifest_sha256
from validate_intermediate_records import canonical_json, strict_json_loads


IMPORTER = "reading-snapshot-importer"
IMPORTER_VERSION = "1"
SNAPSHOT_NAME = "reading-snapshot.json"


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def record_bytes(records: list[dict]) -> bytes:
    return "".join(canonical_json(record) + "\n" for record in records).encode("utf-8")


def _contents(envelope: dict, directory: Path, root: Path, snapshot_bytes: bytes):
    records = envelope["payload"]["records"]
    provenance = envelope["payload"]["provenance"]
    manifest = provenance["input_manifest"]
    fingerprint_payload = {
        "importer": IMPORTER, "importer_version": IMPORTER_VERSION,
        "snapshot_id": envelope["snapshot_id"],
        "snapshot_sha256": digest(snapshot_bytes),
        "source_processing_fingerprint_sha256": provenance["processing_fingerprint"]["sha256"],
    }
    fingerprint = {"version": "1", "payload": fingerprint_payload,
                   "sha256": digest(canonical_json(fingerprint_payload).encode())}
    entries, files, aggregates, totals = {}, {}, {}, {}
    for source in manifest:
        identity = source["document_id"]
        shards = {}
        for kind in RECORD_FILES:
            if kind == "documents":
                selected = [item for item in records[kind] if item["document_id"] == identity]
            elif kind == "evidence":
                selected = [item for item in records[kind] if item["document_id"] == identity]
            else:
                evidence_ids = {item["evidence_id"] for item in records["evidence"] if item["document_id"] == identity}
                selected = [item for item in records[kind] if
                            item["from_ref"]["record_id"] in evidence_ids | {identity}]
            relative = f"shards/{identity}.{kind}.jsonl"
            raw = record_bytes(selected)
            files[relative] = raw
            shards[kind] = {"relative_path": relative, "sha256": digest(raw),
                            "size_bytes": len(raw), "record_count": len(selected)}
        entries[source["relative_path"]] = {
            "relative_path": source["relative_path"], "source_sha256": source["source_sha256"],
            "document_id": identity, "status": source["status"], "shards": shards,
            "processing_fingerprint_sha256": fingerprint["sha256"],
        }
    for kind, filename in RECORD_FILES.items():
        raw = record_bytes(records[kind])
        ordered = b"".join(files[entry["shards"][kind]["relative_path"]] for entry in entries.values())
        if raw != ordered:
            raise ValueError("snapshot record order cannot be materialized losslessly by document")
        files[filename] = raw
        totals[kind] = len(records[kind])
        aggregates[kind] = {
            "relative_path": filename, "sha256": digest(raw), "size_bytes": len(raw),
            "record_count": totals[kind], "ordered_shard_manifest_sha256":
            ordered_shard_manifest_sha256([entry["shards"][kind] for entry in entries.values()]),
        }
    state = {
        "state_version": "reading-snapshot-materialization-v1",
        "extractor": IMPORTER, "extractor_version": IMPORTER_VERSION,
        "build_status": "complete_with_failures" if any(entry["status"] == "failed" for entry in manifest) else "complete",
        "run_at": provenance["run_at"], "source_root": str(root),
        "input_paths": [entry["relative_path"] for entry in manifest],
        "processing_fingerprint": fingerprint, "source_provenance": provenance,
        "snapshot_binding": {
            "relative_path": SNAPSHOT_NAME, "path": str(directory / SNAPSHOT_NAME),
            "sha256": digest(snapshot_bytes), "snapshot_id": envelope["snapshot_id"],
            "payload_sha256": envelope["payload_sha256"],
        },
        "entries": entries, "aggregates": aggregates, "totals": totals,
    }
    return state, files


def validate_materialized_snapshot(directory: Path, source_root: Path | None = None) -> dict:
    """Independently bind every expanded byte and importer field to its JSON."""
    if directory.is_symlink():
        raise ValueError("snapshot materialization directory must not be a symlink")
    directory = directory.resolve(strict=True)
    if (directory / "shards").is_symlink():
        raise ValueError("snapshot materialization shards must not be a symlink")
    state = strict_json_loads(_read_regular(directory / "build-state.json").decode("utf-8"))
    if not isinstance(state, dict) or state.get("extractor") != IMPORTER:
        raise ValueError("not a reading snapshot materialization")
    root_value = state.get("source_root")
    if not isinstance(root_value, str) or not Path(root_value).is_absolute():
        raise ValueError("invalid materialized source root")
    root = Path(root_value).resolve()
    if source_root is not None and root != source_root.resolve(strict=True):
        raise ValueError("snapshot materialization source root mismatch")
    snapshot_path = directory / SNAPSHOT_NAME
    raw = _read_regular(snapshot_path)
    envelope = load_snapshot(snapshot_path)
    expected, files = _contents(envelope, directory, root, raw)
    if canonical_json(state) != canonical_json(expected):
        raise ValueError("snapshot materialization state differs from frozen input")
    for relative, expected_bytes in files.items():
        path = directory / relative
        if not path.resolve(strict=True).is_relative_to(directory):
            raise ValueError("snapshot materialization path escape")
        if _read_regular(path) != expected_bytes:
            raise ValueError(f"snapshot materialization record mismatch: {relative}")
    if _read_regular(snapshot_path) != raw:
        raise ValueError("snapshot changed during materialization validation")
    return envelope


def materialize(snapshot_path: Path, output: Path, source_root: Path,
                input_paths: list[str] | None = None) -> dict:
    root = source_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("snapshot source root must be a directory")
    if output.is_symlink():
        raise ValueError("snapshot materialization output must not be a symlink")
    output = output.resolve()
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("snapshot materialization must be outside source root")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("snapshot materialization requires an empty directory")
    raw = _read_regular(snapshot_path)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied = output / SNAPSHOT_NAME
    descriptor = os.open(copied, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
    envelope = load_snapshot(copied)
    state, files = _contents(envelope, output, root, raw)
    if input_paths is not None and state["input_paths"] != [unicodedata.normalize("NFC", p) for p in input_paths]:
        raise ValueError("snapshot sources differ from the selected input manifest")
    (output / "shards").mkdir(mode=0o700)
    for relative, content in files.items():
        with (output / relative).open("xb") as handle:
            handle.write(content)
    with (output / "build-state.json").open("x", encoding="utf-8") as handle:
        handle.write(canonical_json(state) + "\n")
    validate_materialized_snapshot(output, root)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--input-manifest", type=Path)
    args = parser.parse_args()
    paths = None
    if args.input_manifest:
        manifest = strict_json_loads(_read_regular(args.input_manifest).decode("utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("paths"), list):
            raise ValueError("invalid selection manifest")
        paths = manifest["paths"]
    state = materialize(args.snapshot, args.out, args.source_root, paths)
    print(json.dumps({"extractor": IMPORTER, "build_status": state["build_status"], "totals": state["totals"]}))


if __name__ == "__main__":
    main()
