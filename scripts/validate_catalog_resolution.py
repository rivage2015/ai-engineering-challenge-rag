#!/usr/bin/env python3
"""Validate CatalogResolutionRun records, optionally by full recomputation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from resolve_catalog import (  # noqa: E402
    ResolutionError,
    resolve_catalog_files,
    validate_resolution_record_local,
)
from validate_question_clause_ir import load_json_records  # noqa: E402


def validate_catalog_resolution(
    resolution_path: str | Path,
    *,
    qur_path: str | Path | None = None,
    clause_ir_path: str | Path | None = None,
    entries_path: str | Path | None = None,
    snapshot_path: str | Path | None = None,
) -> list[str]:
    """Return local errors and, when all inputs exist, recomputation errors."""

    try:
        records = load_json_records(Path(resolution_path))
        if len(records) != 1:
            return ["validator v0.1 accepts exactly one CatalogResolutionRun"]
        record = records[0]
        errors = validate_resolution_record_local(record)
        supplied = (qur_path, clause_ir_path, entries_path, snapshot_path)
        if any(value is not None for value in supplied) and not all(
            value is not None for value in supplied
        ):
            errors.append(
                "qur, clause_ir, entries, and snapshot must be supplied together"
            )
        elif all(value is not None for value in supplied):
            expected = resolve_catalog_files(
                qur_path,
                clause_ir_path,
                entries_path,
                snapshot_path,
                generated_at=record["provenance"]["generated_at"],
            )
            if expected != record:
                errors.append("resolution differs from deterministic input recomputation")
        return sorted(set(errors))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, ResolutionError) as exc:
        return [str(exc)]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("resolution", type=Path)
    parser.add_argument("--qur", type=Path)
    parser.add_argument("--clause-ir", type=Path)
    parser.add_argument("--entries", type=Path)
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args(argv)
    errors = validate_catalog_resolution(
        args.resolution,
        qur_path=args.qur,
        clause_ir_path=args.clause_ir,
        entries_path=args.entries,
        snapshot_path=args.snapshot,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok"}, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
